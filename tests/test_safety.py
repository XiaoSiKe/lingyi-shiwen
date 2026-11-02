"""凭证侧的安全约束：一个号一张脸、一个出口、硬锁、单号速率。

这些不是「功能」，是**少挨打的约束**：
- 同一个 key 跨指纹/跨 IP 用 → 服务端会认为是「凭证被盗用」，触发行为风控；
- ret=-6 不是频率问题，冷却必须是 30 分钟级，而不是普通 300s；
- 单号每分钟请求数要有硬上限，靠全局 pacer 不够（pacer 是全局的）。
"""
from __future__ import annotations

import time

import httpx

from wxrc.account_pool import Account, AccountPool
from wxrc.limiter import AdaptivePacer, CircuitBreaker
from wxrc.proxy_pool import ProxyNode, ProxyPool
from wxrc.session import RiskEngine


def make_engine(accounts: list[Account] | None = None, **kw) -> RiskEngine:
    return RiskEngine(
        proxy_pool=kw.pop("proxy_pool", ProxyPool(nodes=[ProxyNode(url="", label="direct")])),
        account_pool=AccountPool(accounts or [], logger=lambda _m: None),
        pacer=AdaptivePacer(base_interval=0.001, jitter_ratio=0.1),
        breaker=kw.pop("breaker", CircuitBreaker(threshold=50, open_seconds=10)),
        bucket_rate=1000,
        max_attempts=kw.pop("max_attempts", 2),
        sleep=lambda _s: None,
        logger=lambda _m: None,
        cool=kw.pop("cool", None),
        **kw,
    )


# --------------------------------------------------------------------------- #
def test_account_identity_is_pinned():
    """同一个号连续取身份：UA / 出口必须完全一致（跨请求不漂移）。"""
    acc = Account(name="a1", uin="u1", key="k1", client="android", device_seed=12345, rpm=600)
    engine = make_engine([acc])

    first = engine._new_identity(want_account=True, exclude=set())
    second = engine._new_identity(want_account=True, exclude=set())

    assert first.device.ua == second.device.ua
    assert first.device.kind == "android"
    assert "Android" in first.device.ua and "MicroMessenger" in first.device.ua
    assert first.proxy.url == second.proxy.url
    assert engine._bindings["a1"][0] is first.device


def test_account_client_kind_is_respected():
    ios = make_engine([Account(name="i", uin="1", key="k", client="ios")])
    pc = make_engine([Account(name="p", uin="1", key="k", client="pc")])
    assert "iPhone" in ios._new_identity(want_account=True, exclude=set()).device.ua
    assert "MicroMessenger" not in pc._new_identity(want_account=True, exclude=set()).device.ua


class _FakeResp:
    def __init__(self, text: str, status: int = 200, url: str = "https://mp.weixin.qq.com/"):
        self.status_code = status
        self.text = text
        self.url = httpx.URL(url)
        self.headers = {"content-type": "application/json"}


class _FakeClient:
    def __init__(self, sink: list[dict], body: str):
        self.sink = sink
        self.body = body

    def request(self, method, url, params=None, data=None, headers=None, timeout=None):
        self.sink.append({"method": method, "url": url, "headers": dict(headers or {})})
        return _FakeResp(self.body)

    def close(self):
        pass


def test_headers_carry_real_credentials():
    """X-WECHAT-KEY / X-WECHAT-UIN 必须是这个号的，不能是随机串。"""
    sink: list[dict] = []
    engine = make_engine(
        [Account(name="a1", uin="1234567890", key="deadbeef" * 4, client="ios")],
        transport_factory=lambda proxy: _FakeClient(sink, '{"ret": 0, "errmsg": "ok"}'),
    )
    engine.get(
        "https://mp.weixin.qq.com/mp/profile_ext",
        params={"action": "getmsg"},
        xhr=True,
        expect="json",
        want_account=True,
    )
    h = sink[0]["headers"]
    assert h["X-WECHAT-KEY"] == "deadbeef" * 4
    assert h["X-WECHAT-UIN"] == "1234567890"
    assert "MicroMessenger" in h["User-Agent"]


def test_hard_lock_on_ret_minus_6(mock, monkeypatch):
    """ret=-6 → 该号 45 分钟级冷却，而不是 300s。"""
    state, base = mock
    state.banned_uins = {"u9"}
    monkeypatch.setattr("wxrc.getmsg.PROFILE_URL", f"{base}/mp/profile_ext")
    acc = Account(name="a1", uin="u9", key="k1", rpm=600)
    engine = make_engine([acc], cool={"rate_hard": 2700}, max_attempts=2)
    before = time.time()

    try:
        engine.get(
            f"{base}/mp/profile_ext",
            params={"action": "getmsg", "key": "k1", "uin": "u9"},
            xhr=True,
            expect="json",
            want_account=True,
        )
    except Exception:
        pass

    assert acc.cool_until - before >= 2000, "ret=-6 必须是长冷却"
    assert acc.risk_hits >= 1


def test_rpm_reserve_spaces_calls():
    pool = AccountPool([Account(name="a1", uin="u1", key="k", rpm=6)])
    acc = pool.lease()
    assert pool.reserve(acc) == 0.0          # 第一次不等待
    wait = pool.reserve(acc)                 # 立刻再要 → 近似 10s
    assert 8.0 <= wait <= 10.1
