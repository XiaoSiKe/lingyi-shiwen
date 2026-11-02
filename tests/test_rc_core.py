"""风控对抗核心的离线验证：全部打本地 mock，不碰真站。

覆盖四件事：
1. 列表通道命中 ret=-3 后换号重放，且账号被正确冷却；
2. 文章页对「非客户端 UA」拦截时，换指纹能过（证明轮换阶梯有效）；
3. 连续验证码 -> 升级真实浏览器；
4. 全池判死 -> 熔断，不会无限重试。
"""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from tools import mock_wx  # noqa: F401  （fixture 在 conftest 里，这里保留以便 IDE 解析）
from wxrc.account_pool import Account, AccountPool
from wxrc.article import parse_article
from wxrc.getmsg import Cursor, ProfileCrawler, build_profile_url, parse_getmsg
from wxrc.guard import Risk, classify_html, classify_json
from wxrc.limiter import AdaptivePacer, CircuitBreaker
from wxrc.proxy_pool import ProxyNode, ProxyPool
from wxrc.session import BreakerOpen, NoIdentity, RiskEngine


# --------------------------------------------------------------------------- #
def make_engine(base: str, accounts: list[Account] | None = None, **kw) -> RiskEngine:
    return RiskEngine(
        proxy_pool=kw.pop("proxy_pool", ProxyPool(nodes=[ProxyNode(url="", label="direct")])),
        account_pool=AccountPool(accounts or [], logger=lambda _m: None),
        pacer=AdaptivePacer(base_interval=0.001, jitter_ratio=0.1, max_factor=2.0),
        breaker=kw.pop("breaker", CircuitBreaker(threshold=20, open_seconds=30)),
        bucket_rate=1000,
        max_attempts=kw.pop("max_attempts", 6),
        captcha_browser_threshold=kw.pop("captcha_browser_threshold", 2),
        sleep=lambda _s: None,
        logger=lambda _m: None,
        rng=random.Random(kw.pop("seed", 7)),
        **kw,
    )


# --------------------------------------------------------------------------- #
def test_getmsg_rotates_account_on_rate(mock, monkeypatch):
    state, base = mock
    monkeypatch.setattr("wxrc.getmsg.PROFILE_URL", f"{base}/mp/profile_ext")
    state.key_limit = 1          # 同一个 key 第 2 次就来 ret=-3

    a1 = Account(name="a1", uin="u1", key="k1")
    a2 = Account(name="a2", uin="u2", key="k2", used_hour=5)  # 让 a1 优先被租到
    engine = make_engine(base, [a1, a2])
    crawler = ProfileCrawler(engine, cursor=Cursor(None), page_pause=(0.001, 0.002))

    items = list(crawler.crawl("MZTESTBIZ==", limit=20, max_pages=4))

    assert len(items) == 16, f"应翻完 2 页共 16 条，实际 {len(items)}"
    # 第 2 页 a1 被 ret=-3 拦下 -> 交回池子冷却 -> 换 a2 重放同一页
    assert a1.risk_hits >= 1 and a1.cool_until > 0, "a1 必须被冷却"
    assert a2.ok >= 1, "必须换到 a2 才拿到第 2 页"
    assert {i.title for i in items} == {f"测试文章 {i}" for i in range(16)}


def test_article_fingerprint_rotation_beats_ua_block(mock, monkeypatch):
    """mock 只放行微信客户端 UA：第 1 次强制用桌面 UA 必被 302，换指纹后过。"""
    _state, base = mock
    from wxrc import session as session_mod
    from wxrc.fingerprint import make_device

    calls = {"n": 0}

    def fake_device(kind=None, rng=None):
        calls["n"] += 1
        return make_device("desktop" if calls["n"] == 1 else "android", rng)

    monkeypatch.setattr(session_mod, "make_device", fake_device)

    # 两个显式配置的模拟出口；不依赖冷却后创建隐式直连节点。
    import httpx
    engine = make_engine(
        base, [],
        proxy_pool=ProxyPool(nodes=[ProxyNode(url="mock://a"), ProxyNode(url="mock://b")]),
        transport_factory=lambda _proxy: httpx.Client(follow_redirects=True, trust_env=False),
    )
    reply = engine.get(f"{base}/s/mp-only-1", expect="html")

    assert reply.verdict.ok, reply.verdict
    assert engine.stats["rotations"] == 1, "第一次被 UA 拦，第二次换指纹才该成功"
    assert "js_content" in reply.text


def test_captcha_escalates_to_browser(mock):
    state, base = mock
    state.force_captcha_paths.add("/s/blocked")

    seen: list[str] = []

    def browser(url: str, referer: str):
        seen.append(url)
        html = (
            "<html><body><h1 id='activity-name'>浏览器救回</h1>"
            "<div id='js_content'><p>正文</p></div></body></html>"
        )
        return 200, html, url

    engine = make_engine(base, [], browser_hook=browser, captcha_browser_threshold=1)
    reply = engine.get(f"{base}/s/blocked", expect="html")

    assert reply.via == "browser"
    assert engine.stats["browser_escalations"] == 1
    assert seen and "blocked" in seen[0]


def test_dead_accounts_open_breaker(mock):
    state, base = mock
    state.dead_keys.add("dead-1")
    acc = Account(name="zombie", uin="u9", key="dead-1")
    engine = make_engine(
        base, [acc], max_attempts=3, breaker=CircuitBreaker(threshold=1, open_seconds=60)
    )
    with pytest.raises((NoIdentity, BreakerOpen)):
        engine.get(
            f"{base}/mp/profile_ext",
            params={"action": "getmsg", "key": "dead-1", "uin": "u9", "offset": "0"},
            expect="json",
            want_account=True,
        )
    with pytest.raises(BreakerOpen):
        engine.get(
            f"{base}/mp/profile_ext",
            params={"action": "getmsg", "key": "dead-1", "uin": "u9", "offset": "0"},
            expect="json",
            want_account=True,
        )


def test_parse_getmsg_flat_structure():
    payload = {
        "ret": 0,
        "next_offset": 8,
        "can_msg_continue": 1,
        "general_msg_list": (
            '{"list":[{"comm_msg_info":{"datetime":1750000000},'
            '"app_msg_ext_info":{"title":"头条","content_url":"https://mp.weixin.qq.com/s?__biz=B==&mid=1&idx=1&sn=x",'
            '"multi_app_msg_item_list":[{"title":"次条","content_url":"https://mp.weixin.qq.com/s?__biz=B==&mid=1&idx=2&sn=y"}]}}]}'
        ),
    }
    out = parse_getmsg(payload)
    assert out["can_continue"] is True and out["next_offset"] == 8
    assert [i.title for i in out["items"]] == ["头条", "次条"]
    assert out["items"][1].idx == "2"

    base, params = build_profile_url("B==", Account(name="a", uin="123", key="k"), 8)
    assert params["action"] == "getmsg" and params["offset"] == "8" and params["__biz"] == "B=="


def test_guard_classification():
    assert classify_json({"ret": -3}).risk is Risk.RATE
    assert classify_json({"ret": 200003}).risk is Risk.CRED
    assert classify_json({"ret": 0}).ok
    v = classify_html(
        "<html><body>当前环境异常，完成验证后即可继续访问</body></html>",
        url="https://mp.weixin.qq.com/s/x",
    )
    assert v.risk is Risk.CAPTCHA and v.rotate_ip
    assert classify_html(
        "<html><body>该内容已被发布者删除</body></html>", url="https://mp.weixin.qq.com/s/x"
    ).risk is Risk.GONE


def test_archive_job_end_to_end(mock, monkeypatch, tmp_path):
    """整条链路：列表 -> 正文 -> Markdown/raw/index.jsonl，并验证断点去重。"""
    import json

    from wxrc.crawl import ArchiveJob

    state, base = mock
    state.ua_limit = 100  # 此用例只验证归档与恢复，限流暂停由独立回归覆盖。
    from wxrc.fingerprint import make_device
    monkeypatch.setattr("wxrc.session.make_device", lambda kind=None, rng=None: make_device("android", rng))
    monkeypatch.setattr("wxrc.getmsg.PROFILE_URL", f"{base}/mp/profile_ext")
    acc = Account(name="a1", uin="u1", key="k1", quota_hour=500)
    engine = make_engine(base, [acc])
    job = ArchiveJob(engine, tmp_path, article_pause=(0.001, 0.002))

    stats = job.run("MZTESTBIZ==", limit=20, max_pages=4)
    assert stats.listed == 16 and stats.fetched == 16, stats.as_dict()

    mds = sorted(tmp_path.glob("**/*.md"))
    assert len(mds) == 16
    text = mds[0].read_text("utf-8")
    assert "source: " in text and "# 测试文章" in text

    recs = [json.loads(x) for x in (tmp_path / "index.jsonl").read_text("utf-8").splitlines()]
    assert len(recs) == 16 and all(r["status"] == "ok" for r in recs)

    # 再跑一次：游标已 done，不应重复抓
    stats2 = job.run("MZTESTBIZ==", limit=20, max_pages=4)
    assert stats2.listed == 0 and stats2.fetched == 0


FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "wechat-mp-collector"
    / "fixtures"
    / "article_ni3KysbNVM8LCcEGDB_1YA.html"
)


@pytest.mark.skipif(not FIXTURE.exists(), reason="缺少真机 fixture")
def test_parse_real_article():
    raw = FIXTURE.read_text("utf-8", errors="replace")
    art = parse_article(raw, "https://mp.weixin.qq.com/s/ni3KysbNVM8LCcEGDB_1YA")
    assert art.title
    assert art.account
    assert art.publish_ts > 1_600_000_000
    assert len(art.content_text) > 500
    assert art.images and art.images[0].startswith("https://mmbiz.qpic.cn")
    assert "# " not in art.markdown.splitlines()[0] or art.markdown
