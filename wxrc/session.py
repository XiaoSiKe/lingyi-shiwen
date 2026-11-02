"""零一拾文请求引擎。

通道通过 account 显式传入账号；引擎统一处理身份绑定、逐请求配额、限速和冷却。
公开正文可在已配置且可用的出口间重试；出口耗尽时暂停，不创建隐式直连。
每次重试发送前检查熔断，浏览器兜底仅重放匿名 GET 正文并继承当前出口。
"""
from __future__ import annotations

import hashlib
import json as _json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict

import httpx

from .account_pool import Account, AccountPool
from .fingerprint import Device, make_device
from .guard import Risk, Verdict, classify_html, classify_json
from .limiter import AdaptivePacer, CircuitBreaker, TokenBucket
from .proxy_pool import ProxyNode, ProxyPool
from .security import redact


class RiskEngineError(RuntimeError):
    """引擎级异常（不是单次请求失败）。"""


class BreakerOpen(RiskEngineError):
    def __init__(self, wait: float):
        super().__init__(f"熔断中，建议等待 {wait:.0f}s")
        self.wait = wait


class NoIdentity(RiskEngineError):
    pass


class Exhausted(RiskEngineError):
    def __init__(self, attempts: int, last: "Reply | None"):
        super().__init__(f"{attempts} 次尝试后仍被风控")
        self.attempts = attempts
        self.last = last


# --------------------------------------------------------------------------- #
@dataclass
class Identity:
    device: Device
    proxy: ProxyNode
    account: Account | None = None
    wx_key: str = ""
    wx_uin: str = ""

    @property
    def name(self) -> str:
        return redact(f"{self.account.name if self.account else '-'}|{self.proxy.name}")

    def cookie(self) -> str:
        return self.account.cookie() if self.account else ""


@dataclass
class Reply:
    status: int
    text: str
    url: str
    final_url: str
    headers: Dict[str, str] = field(default_factory=dict)
    payload: dict | None = None
    verdict: Verdict = field(default_factory=Verdict)
    identity: str = ""
    attempts: int = 1
    elapsed: float = 0.0
    via: str = "httpx"

    def json(self) -> dict:
        if self.payload is not None:
            return self.payload
        return _json.loads(self.text)


DEFAULT_COOL = {"rate": 240.0, "cred": 1800.0, "captcha": 900.0, "login": 1800.0}


class RiskEngine:
    def __init__(
        self,
        *,
        proxies: list[str] | None = None,
        accounts: list[Account] | None = None,
        proxy_pool: ProxyPool | None = None,
        account_pool: AccountPool | None = None,
        pacer: AdaptivePacer | None = None,
        breaker: CircuitBreaker | None = None,
        cool: dict[str, float] | None = None,
        timeout: float = 20.0,
        max_attempts: int = 8,
        bucket_rate: float = 0.45,
        verify: bool = True,
        transport_factory: Callable[[str], Any] | None = None,
        browser_hook: Callable[[str, str], tuple[int, str, str] | None] | None = None,
        captcha_browser_threshold: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        logger: Callable[[str], None] | None = None,
        rng: random.Random | None = None,
    ):
        self.rng = rng or random.Random()
        sink = logger or (lambda _m: None)
        self.log = lambda message: sink(redact(message))
        self.sleep = sleep
        self.timeout = timeout
        self.max_attempts = max(1, int(max_attempts))
        self.verify = verify
        self.bucket_rate = max(1e-3, float(bucket_rate))
        self.cool = {**DEFAULT_COOL, **(cool or {})}
        self.browser_hook = browser_hook
        self.captcha_browser_threshold = captcha_browser_threshold

        self.proxies = proxy_pool or ProxyPool(
            nodes=[ProxyNode(url=u, label=(u or "direct")) for u in (proxies or [""])]
        )
        self.accounts = account_pool or AccountPool(accounts or [], logger=self.log)
        self.pacer = pacer or AdaptivePacer()
        self.breaker = breaker or CircuitBreaker()
        self._buckets: dict[str, TokenBucket] = {}
        self._clients: dict[str, Any] = {}
        # 账号 -> (device, proxy_url)：一个号一张脸、一个出口，跨请求保持不变
        self._bindings: dict[str, tuple[Device, str]] = {}
        self._transport = transport_factory or self._default_transport
        self._captcha_streak = 0
        self.stats = {
            "requests": 0,
            "ok": 0,
            "soft": 0,
            "rate": 0,
            "cred": 0,
            "captcha": 0,
            "login": 0,
            "gone": 0,
            "rotations": 0,
            "browser_escalations": 0,
        }

    # ------------------------------------------------------------------ #
    # 传输层
    # ------------------------------------------------------------------ #
    def _default_transport(self, proxy: str) -> httpx.Client:
        kwargs: dict[str, Any] = dict(
            timeout=self.timeout,
            follow_redirects=True,   # 验证页在 redirect 后面，必须跟过去才能判定
            verify=self.verify,
            trust_env=False,
        )
        if proxy:
            kwargs["proxy"] = proxy
        return httpx.Client(**kwargs)

    def _bucket(self, node: ProxyNode) -> TokenBucket:
        b = self._buckets.get(node.name)
        if b is None:
            b = TokenBucket(rate=self.bucket_rate, burst=1.0)  # 单出口硬上限
            self._buckets[node.name] = b
        return b

    def _client(self, node: ProxyNode) -> Any:
        """按出口复用连接池：既省 TLS 握手，也更像真人（真人不会每次新建 TCP）。"""
        key = node.name
        client = self._clients.get(key)
        if client is None:
            client = self._transport(node.url)
            self._clients[key] = client
        return client

    def _new_identity(self, *, want_account: bool, exclude: set[str], biz: str = "", account: Account | None = None) -> Identity:
        if want_account and account is None:
            # 号池耗尽时回落到「无号」模式（有的通道只要 cookie 不要 key）
            account = self.accounts.select(exclude=exclude, biz=biz)
            if account is None and not self.accounts.accounts:
                account = None
            elif account is None:
                raise NoIdentity("号池中无可用账号（全部冷却/配额用尽/判死）")

        if account is not None:
            # 凭证模式：指纹与出口都锁死在这个账号上，绝不跨号复用，也不随机漂移
            bound = self._bindings.get(account.name)
            if bound is None:
                node = self.proxies.acquire(exclude=exclude)
                if node is None:
                    raise NoIdentity("配置的出口全部不可用；任务暂停，不自动直连")
                seed = int(account.device_seed) or int(hashlib.sha256(account.name.encode()).hexdigest()[:8], 16)
                device = make_device(account.client or None, rng=random.Random(seed))
                self._bindings[account.name] = (device, node.url)
            else:
                device, proxy_url = bound
                node = self.proxies.node_for(proxy_url)
                if not node.available() or node.score <= self.proxies.min_score:
                    raise NoIdentity("账号绑定的出口正在冷却；任务暂停")
            return Identity(
                device=device,
                proxy=node,
                account=account,
                wx_key=account.key or "",
                wx_uin=account.uin or "",
            )

        node = None
        for _ in range(len(self.proxies.nodes) + 1):
            node = self.proxies.acquire(exclude=exclude)
            if node is not None:
                break
        if node is None:
            raise NoIdentity("配置的出口全部不可用；任务暂停，不自动直连")
        return Identity(
            device=make_device(rng=self.rng),
            proxy=node,
            account=None,
            wx_key="",
            wx_uin="",
        )

    def _headers(self, ident: Identity, *, url: str, referer: str, xhr: bool) -> dict[str, str]:
        h = ident.device.headers(referer=referer, cookie=ident.cookie(), xhr=xhr)
        if xhr and ident.account:
            # 有账号时必须用这个号自己的 key/uin，随机会立刻暴露“不是同一个客户端”
            h["X-WECHAT-KEY"] = (ident.account.key if ident.account else "") or ident.wx_key
            h["X-WECHAT-UIN"] = (ident.account.uin if ident.account else "") or ident.wx_uin
            h["Origin"] = "https://mp.weixin.qq.com"
        return h

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
        data: Any = None,
        referer: str = "https://mp.weixin.qq.com/",
        xhr: bool = False,
        expect: str = "html",     # html | json
        want_account: bool = False,
        account: Account | None = None,
        biz: str = "",
        max_attempts: int | None = None,
        raise_on_exhaust: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> Reply:
        allowed, wait = self.breaker.allow()
        if not allowed:
            raise BreakerOpen(wait)

        attempts = max_attempts or self.max_attempts
        exclude: set[str] = set()
        ident = self._new_identity(want_account=want_account, exclude=exclude, biz=biz, account=account)
        last: Reply | None = None
        started = time.monotonic()
        soft_budget = 2

        for attempt in range(1, attempts + 1):
            allowed, wait = self.breaker.allow()
            if not allowed:
                raise BreakerOpen(wait)
            self.pacer.wait()
            bucket = self._bucket(ident.proxy)
            gap = bucket.take()
            if gap > 0:
                self.sleep(gap)

            if ident.account:
                if not self.accounts.charge(ident.account):
                    raise NoIdentity("当前账号已冷却、失效或配额用尽")
                delay = self.accounts.reserve(ident.account)
                if delay > 0:
                    self.sleep(delay)
            allowed, wait = self.breaker.allow()
            if not allowed:
                raise BreakerOpen(wait)
            headers = self._headers(ident, url=url, referer=referer, xhr=xhr)
            if extra_headers:
                headers.update(extra_headers)
            reply = self._send(
                ident, url, params=params, headers=headers, expect=expect, method=method, data=data
            )
            reply.attempts = attempt
            reply.elapsed = time.monotonic() - started
            last = reply
            self.stats["requests"] += 1
            risk = reply.verdict.risk
            self.stats[risk.value if risk.value in self.stats else "soft"] += 1

            # ---- 正常 ---- #
            if risk is Risk.OK:
                self.breaker.success()
                self.pacer.on_success()
                if ident.account:
                    self.accounts.report(ident.account, "ok")
                self.proxies.report(ident.proxy, ok=True)
                self._captcha_streak = 0
                return reply

            if risk is Risk.GONE:
                return reply
            self.breaker.failure()
            self.log(f"[risk] {risk.value} {reply.verdict.reason} <- {url[:90]}")

            # ---- 软失败：同身份重试 ---- #
            if risk is Risk.SOFT and soft_budget > 0:
                soft_budget -= 1
                self.pacer.on_risk(1.3)
                self.sleep(min(30.0, 2.0 ** (2 - soft_budget)) * self.rng.uniform(0.8, 1.3))
                continue

            # ---- 风控类：按阶梯换身份 ---- #
            if risk is Risk.CAPTCHA:
                self.pacer.on_risk(3.0)
                self._captcha_streak += 1
                self.proxies.report(ident.proxy, ok=False, risk="captcha")
                if ident.account:
                    self.accounts.report(ident.account, "captcha", cool=self.cool)
            elif risk is Risk.RATE:
                self.pacer.on_risk(2.2)
                if ident.account is None or reply.verdict.rotate_ip:
                    self.proxies.report(ident.proxy, ok=False, risk="rate")
                if ident.account:
                    # ret=-6 是行为风控：单号长冷却，不能当成普通频率问题
                    hard = "ret=-6" in reply.verdict.reason or "-6" in reply.verdict.evidence
                    self.accounts.report(
                        ident.account, "rate_hard" if hard else "rate", cool=self.cool
                    )
            elif risk in (Risk.CRED, Risk.LOGIN):
                self.pacer.on_risk(1.4)
                if ident.account:
                    self.accounts.report(ident.account, "cred", cool=self.cool)
                self.proxies.report(ident.proxy, ok=True)

            # 升级浏览器：同一出口连续吃验证码，换头换 IP 都不够
            if (
                risk is Risk.CAPTCHA
                and self.browser_hook is not None
                and ident.account is None
                and not xhr and expect == "html" and method.upper() == "GET"
                and (self._captcha_streak >= self.captcha_browser_threshold
                     or not any(n.available() for n in self.proxies.nodes))
            ):
                browser_url = str(httpx.URL(url, params=params)) if params else url
                got = self._escalate_browser(browser_url, referer, expect, proxy_url=ident.proxy.url)
                if got is not None:
                    return got

            # 决定这一轮排除哪一层
            exclude_this = set()
            if risk in (Risk.CRED, Risk.LOGIN) and ident.account:
                exclude_this.add(ident.account.name)
            elif risk is Risk.RATE and ident.account and self.rng.random() < 0.6:
                exclude_this.add(ident.account.name)
            elif risk is Risk.CAPTCHA:
                exclude_this.add(ident.proxy.name)
                if ident.account and self.rng.random() < 0.3:
                    exclude_this.add(ident.account.name)

            # 显式账号由通道选择，不能偷偷换身份后继续携带旧凭证。
            if account is not None or attempt >= attempts:
                break
            allowed, wait = self.breaker.allow()
            if not allowed:
                raise BreakerOpen(wait)

            cool_left = max(0.0, reply.verdict.retry_after)
            pause = min(cool_left, 90.0) * self.rng.uniform(0.6, 1.2)
            if pause > 0:
                self.sleep(pause)

            exclude |= exclude_this
            try:
                ident = self._new_identity(want_account=want_account, exclude=exclude, biz=biz, account=account)
                self.stats["rotations"] += 1
                self.log(f"[rotate] -> {ident.name} (排除 {sorted(exclude)})")
            except NoIdentity:
                # 全池不可用：开熔断，交给上层决定等还是换通道
                self.breaker.failure()
                raise NoIdentity("所有身份都不可用，建议等冷却或换采集通道")

        if raise_on_exhaust:
            raise Exhausted(attempts, last)
        return last  # type: ignore[return-value]

    # ------------------------------------------------------------------ #
    def _send(
        self,
        ident: Identity,
        url: str,
        *,
        params: dict[str, Any] | None,
        headers: dict[str, str],
        expect: str,
        method: str = "GET",
        data: Any = None,
    ) -> Reply:
        client = self._client(ident.proxy)
        try:
            resp = client.request(
                method.upper(),
                url,
                params=params,
                data=data,
                headers=headers,
                timeout=self.timeout,
            )
            status = int(resp.status_code)
            text = resp.text if hasattr(resp, "text") else ""
            final_url = str(resp.url)
            hdrs = {str(k).lower(): str(v) for k, v in dict(resp.headers).items()}
        except Exception as exc:
            # 连接坏了就丢掉这个客户端的连接池，下次重建
            self._clients.pop(ident.proxy.name, None)
            try:
                client.close()
            except Exception:
                pass
            return Reply(
                status=0,
                text="",
                url=url,
                final_url=url,
                verdict=Verdict(Risk.SOFT, f"{type(exc).__name__}: {exc}", retry_after=5.0),
                identity=ident.name,
            )

        payload = None
        verdict: Verdict
        location = hdrs.get("location", "")
        if status >= 400 or 300 <= status < 400:
            verdict = classify_html(text, url=url, final_url=final_url, status=status, location=location, cool=self.cool)
            if expect == "json":
                payload = _safe_json(text)
        elif expect == "json":
            try:
                payload = _json.loads(text)
            except ValueError:
                payload = None
            if payload is None:
                verdict = classify_html(
                    text, url=url, final_url=final_url, status=status, location=location, cool=self.cool
                )
                if verdict.ok:
                    verdict = Verdict(Risk.SOFT, "期望 JSON 但拿到非 JSON", retry_after=5.0)
            else:
                verdict = classify_json(payload, cool=self.cool)
                if not verdict.ok and verdict.risk is Risk.RATE and status in (403, 429):
                    verdict.rotate_ip = True
        else:
            verdict = classify_html(
                text, url=url, final_url=final_url, status=status, location=location, cool=self.cool
            )

        # 服务端给的 Retry-After 优先
        ra = hdrs.get("retry-after")
        if ra:
            try:
                verdict.retry_after = max(verdict.retry_after, float(ra))
            except ValueError:
                pass

        return Reply(
            status=status,
            text=text,
            url=url,
            final_url=final_url,
            headers=hdrs,
            payload=payload,
            verdict=verdict,
            identity=ident.name,
            via="httpx",
        )

    def _escalate_browser(self, url: str, referer: str, expect: str, *, proxy_url: str = "") -> Reply | None:
        """交给真实浏览器渲染（人工过一次验证码后，拿回可复用 cookie）。"""
        assert self.browser_hook is not None
        if proxy_url and not getattr(self.browser_hook, "supports_proxy", False):
            self.log("[browser] 当前浏览器适配器不能继承出口；暂停，不改为直连")
            return None
        self.log(f"[browser] 升级渲染：{url[:100]}")
        try:
            if getattr(self.browser_hook, "supports_proxy", False):
                got = self.browser_hook(url, referer, proxy_url=proxy_url)
            else:
                got = self.browser_hook(url, referer)
        except Exception as exc:
            self.log(f"[browser] 失败：{exc}")
            return None
        if not got:
            return None
        status, text, final_url = got
        verdict = (
            classify_json(_safe_json(text), cool=self.cool) if expect == "json"
            else classify_html(text, url=url, final_url=final_url, status=status, cool=self.cool)
        )
        self.stats["browser_escalations"] += 1
        self._captcha_streak = 0
        if verdict.ok:
            self.breaker.success()
        return Reply(
            status=status,
            text=text,
            url=url,
            final_url=final_url,
            payload=_safe_json(text) if expect == "json" else None,
            verdict=verdict,
            identity="browser",
            via="browser",
        )

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        return {
            "stats": dict(self.stats),
            "pacer": self.pacer.snapshot(),
            "proxies": self.proxies.snapshot(),
            "accounts": self.accounts.snapshot(),
        }

    def close(self) -> None:
        for client in self._clients.values():
            try:
                client.close()
            except Exception:
                pass
        self._clients.clear()
        close_hook = getattr(self.browser_hook, "close", None)
        if close_hook:
            close_hook()


def _safe_json(text: str) -> dict | None:
    try:
        return _json.loads(text)
    except ValueError:
        return None
