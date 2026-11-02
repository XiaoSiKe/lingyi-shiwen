"""指纹内核桥的验证：直接调 零一智译/browser-core，跑真实 Chrome。

三个断言层次：
1. 协议层：Python ↔ Node 桥的 JSON 行协议能起、能 ping、能关；
2. 采集层：能从本地页取回 HTML / 状态 / cookie，且注入生效（webdriver=false、UA 无 HeadlessChrome）；
3. 指纹层：不同 profile_key → 不同 canvas 噪声（这正是「一账号一画像」的可用性证明）。
"""
from __future__ import annotations

import pytest

from wxrc import kernel_bridge
from wxrc.kernel_bridge import KernelBridge, profile_id_for

CORE = kernel_bridge.default_core_path()
NODE = kernel_bridge.node_path()

pytestmark = pytest.mark.skipif(
    not (CORE and NODE), reason="需要 node + 零一智译/browser-core（LINGYI_BROWSER_CORE 可指定）"
)


def _bridge() -> KernelBridge:
    return KernelBridge(core_path=CORE, headless=True, logger=lambda _m: None)


def test_bridge_protocol_ping():
    with _bridge() as bridge:
        info = bridge.ping()
    assert info["pong"] is True
    assert info["node"].startswith("v")


def test_profile_id_is_stable():
    assert profile_id_for("MZABC==") == profile_id_for("MZABC==")
    assert profile_id_for("MZABC==") != profile_id_for("MZDEF==")
    assert profile_id_for("x").startswith("p_") and len(profile_id_for("x")) == 10


def test_open_local_page_with_injection(mock):
    _state, base = mock
    with _bridge() as bridge:
        data = bridge.open(
            f"{base}/plain",
            profile_key="probe-a",
            seed=1234,
            locale="zh-CN",
            wait_human_ms=0,
            total_timeout=180,
        )
        fp = data["fingerprint"]
        cookies = bridge.cookies([f"{base}/plain"])

    assert "wxrc kernel probe" in data["html"]
    assert data["status"] == 200
    assert fp["webdriver"] is False, "headless 下必须被 env shim 修正回 false"
    assert "HeadlessChrome" not in (fp["ua"] or "")
    assert "Chrome/" in (fp["ua"] or "")
    assert fp["platform"] == "Win32"
    assert fp["timezone"] == "Asia/Shanghai"
    assert (fp["languages"] or [""])[0].startswith("zh")
    assert fp["canvasHash"], "注入层应该给出稳定的 canvas 哈希"
    assert isinstance(cookies, list)


def test_two_profiles_diverge(mock):
    """同机、同 locale，不同 seed → canvas 哈希必须不同（跨环境高熵差异）。"""
    _state, base = mock
    with _bridge() as bridge:
        a = bridge.open(f"{base}/plain", profile_key="probe-a", seed=111, wait_human_ms=0, total_timeout=180)
        b = bridge.open(f"{base}/plain", profile_key="probe-b", seed=222, wait_human_ms=0, total_timeout=180)

    fa, fb = a["fingerprint"], b["fingerprint"]
    assert fa["canvasHash"] != fb["canvasHash"]
    assert fa["hardwareConcurrency"] is not None and fb["hardwareConcurrency"] is not None
    assert a["profileId"] != b["profileId"]


def test_engine_escalates_to_fingerprint_browser(mock):
    """HTTP 层永远过不去（缺 Sec-Fetch-User）→ 引擎升级指纹浏览器 → 拿到正文。"""
    from wxrc.browser import make_kernel_hook
    from wxrc.session import RiskEngine

    _state, base = mock
    hook = make_kernel_hook(
        core_path=CORE,
        headless=True,
        profile_key="probe-a",
        wait_human_ms=0,
        logger=lambda _m: None,
    )
    engine = RiskEngine(
        proxies=[""],
        accounts=[],
        browser_hook=hook,
        captcha_browser_threshold=1,
        max_attempts=3,
        bucket_rate=1000,
        logger=lambda _m: None,
    )
    try:
        reply = engine.get(f"{base}/s/browser-only", expect="html")
    finally:
        hook.close()

    assert reply.via == "browser"
    assert reply.verdict.ok, reply.verdict
    assert "js_content" in reply.text
    assert engine.stats["browser_escalations"] == 1
