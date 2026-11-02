"""号池：uin / key / pass_ticket / appmsg_token / wap_sid2 的租借与配额。

微信客户端通道的凭证 30 分钟一轮（key 会过期），且每个号的「行为额度」是有限的。
所以号池要管四件事：

1. 租借（lease）：跳过冷却中、配额耗尽、被判死的号；
2. 配额（quota）：小时/日双窗口，命中频率风控时按比例降额（自适应缩容）；
3. 冷却（cooldown）：ret=-3 冷却几分钟，ret=-6 冷却更久，200003 直接判需要续期；
4. 续期（refresh_hook）：key 过期时回调外部刷新（浏览器扫码 / 客户端抓包 / 上游池），
   刷新失败就标记 dead，整个链路自然降级到别的通道。

凭证结构（账号池 JSON 一行一个）：
{"name":"a1","uin":"...","key":"...","pass_ticket":"...","appmsg_token":"...",
 "wap_sid2":"...","biz":"MzA...==","quota_hour":60,"quota_day":400}
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .security import redact


@dataclass
class Account:
    name: str
    uin: str = ""
    key: str = ""
    pass_ticket: str = ""
    appmsg_token: str = ""
    wap_sid2: str = ""
    biz: str = ""
    quota_hour: int = 60
    quota_day: int = 400
    client: str = "ios"          # ios | android | pc —— 必须与产出 key 的客户端一致
    device_seed: int = 0         # 固定指纹种子（0 = 用 name 派生），保证一个号一张脸
    rpm: int = 12                # 单号每分钟上限

    # ---- 运行时状态 ----
    used_hour: int = 0
    used_day: int = 0
    hour_start: float = field(default_factory=time.time)
    day_start: float = field(default_factory=time.time)
    cool_until: float = 0.0
    key_issued_at: float = field(default_factory=time.time)
    key_ttl: float = 1800.0          # 微信客户端 key 约 30 分钟
    dead: str = ""                   # 非空 = 判死原因
    risk_hits: int = 0
    ok: int = 0
    last_issue: float = 0.0
    shrink: float = 1.0              # 命中风控后的配额缩容系数

    # ------------------------------------------------------------ #
    def cookie(self) -> str:
        parts = []
        if self.wap_sid2:
            parts.append(f"wap_sid2={self.wap_sid2}")
        if self.uin:
            parts.append(f"wxuin={self.uin}")
        return "; ".join(parts)

    @property
    def key_age(self) -> float:
        return time.time() - self.key_issued_at

    @property
    def key_stale(self) -> bool:
        return self.key_age > self.key_ttl

    def available(self, now: float | None = None) -> bool:
        now = now or time.time()
        if self.dead:
            return False
        if now < self.cool_until:
            return False
        self._roll_windows(now)
        if self.used_hour >= self.quota_hour * self.shrink:
            return False
        if self.used_day >= self.quota_day * self.shrink:
            return False
        return True

    def _roll_windows(self, now: float) -> None:
        if now - self.hour_start >= 3600:
            self.used_hour = 0
            self.hour_start = now
        if now - self.day_start >= 86400:
            self.used_day = 0
            self.day_start = now


class AccountPool:
    # rate_hard 对应 ret=-6（行为风控）：不是「慢一点」，而是这个号几十分钟不能再动
    COOL = {"rate": 300.0, "rate_hard": 2700.0, "cred": 1800.0, "login": 1800.0, "captcha": 600.0}

    def __init__(
        self,
        accounts: list[Account],
        *,
        refresh_hook: Callable[[Account], bool] | None = None,
        logger: Callable[[str], None] | None = None,
    ):
        self.accounts = accounts or []
        self.refresh_hook = refresh_hook
        sink = logger or (lambda _m: None)
        self.log = lambda message: sink(redact(message))
        self._lock = threading.Lock()

    # ------------------------------------------------------------ #
    def lease(self, exclude: set[str] | None = None, biz: str = "") -> Account | None:
        return self.select(exclude=exclude, biz=biz, charge=True)

    def select(self, exclude: set[str] | None = None, biz: str = "", *, charge: bool = False) -> Account | None:
        exclude = exclude or set()
        now = time.time()
        with self._lock:
            cands = [a for a in self.accounts if a.name not in exclude and a.available(now)]
            # 优先「用得最少」的号，摊平单号行为曲线
            cands.sort(key=lambda a: (a.used_hour / max(1, a.quota_hour), -a.ok))
            for acc in cands:
                if biz and acc.biz and acc.biz != biz:
                    continue
                if acc.key_stale and self.refresh_hook and not self._refresh(acc):
                    continue
                if charge:
                    acc.used_hour += 1
                    acc.used_day += 1
                return acc
            return None

    def charge(self, acc: Account) -> bool:
        """实际发送前每次计费；外部显式账号同样遵守可用性与配额。"""
        with self._lock:
            if not acc.available():
                return False
            acc.used_hour += 1
            acc.used_day += 1
            return True

    def reserve(self, acc: Account) -> float:
        """按 rpm 预约下一次调用，返回需要等待的秒数（0 表示可以立刻用）。"""
        with self._lock:
            now = time.time()
            gap = 60.0 / max(1, int(acc.rpm or 1))
            wait = max(0.0, acc.last_issue + gap - now)
            acc.last_issue = now + wait
            return wait

    def _refresh(self, acc: Account) -> bool:
        try:
            ok = bool(self.refresh_hook(acc)) if self.refresh_hook else False
        except Exception as exc:  # 刷新通道自身故障不能拖垮主流程
            self.log(f"[account] {acc.name} 续期异常: {exc}")
            ok = False
        if ok:
            acc.key_issued_at = time.time()
            acc.risk_hits = max(0, acc.risk_hits - 1)
            self.log(f"[account] {acc.name} 凭证已续期")
        else:
            acc.dead = "凭证续期失败"
            self.log(f"[account] {acc.name} 判死：{acc.dead}")
        return ok

    def report(self, acc: Account, risk: str, *, cool: dict[str, float] | None = None) -> None:
        """risk: ok / rate / cred / captcha / login"""
        now = time.time()
        table = {**self.COOL, **(cool or {})}
        with self._lock:
            if risk == "ok":
                acc.ok += 1
                # 连续成功缓慢恢复配额
                acc.shrink = min(1.0, acc.shrink + 0.02)
                return
            acc.risk_hits += 1
            acc.shrink = max(0.25, acc.shrink * 0.8)
            if risk in ("cred", "login"):
                acc.cool_until = now + table.get("cred", 1800.0)
                if self.refresh_hook:
                    self._refresh(acc)
                else:
                    acc.dead = "凭证失效且无续期通道"
            elif risk == "rate":
                acc.cool_until = now + table.get("rate", 300.0)
            elif risk == "rate_hard":
                acc.cool_until = now + table.get("rate_hard", 2700.0)
            elif risk == "captcha":
                acc.cool_until = now + table.get("captcha", 600.0)

    def mark_dead(self, acc: Account, reason: str) -> None:
        acc.dead = reason

    def snapshot(self) -> list[dict]:
        now = time.time()
        return [
            {
                "name": a.name,
                "ok": a.ok,
                "risk": a.risk_hits,
                "used_hour": a.used_hour,
                "cool_left": int(max(0.0, a.cool_until - now)),
                "dead": a.dead,
                "shrink": round(a.shrink, 2),
                "key_age": int(a.key_age),
            }
            for a in self.accounts
        ]

    @property
    def alive(self) -> int:
        return sum(1 for a in self.accounts if not a.dead)
