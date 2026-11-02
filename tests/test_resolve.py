"""名字 → biz 的解析，以及按日期区间过滤列表。"""
from __future__ import annotations

import random

import pytest

from wxrc.account_pool import Account, AccountPool
from wxrc.getmsg import Cursor, ProfileCrawler
from wxrc.limiter import AdaptivePacer, CircuitBreaker
from wxrc.proxy_pool import ProxyNode, ProxyPool
from wxrc.resolve import decode_sogou_hop, parse_sogou_links, parse_sogou_titles
from wxrc.session import RiskEngine

BASE_TS = 1750000000
DAY = 86400


def make_engine(accounts=None) -> RiskEngine:
    return RiskEngine(
        proxy_pool=ProxyPool(nodes=[ProxyNode(url="", label="direct")]),
        account_pool=AccountPool(accounts or [], logger=lambda _m: None),
        pacer=AdaptivePacer(base_interval=0.001, jitter_ratio=0.1),
        breaker=CircuitBreaker(threshold=50, open_seconds=10),
        bucket_rate=1000,
        max_attempts=3,
        sleep=lambda _s: None,
        logger=lambda _m: None,
        rng=random.Random(11),
    )


# --------------------------------------------------------------------------- #
# 搜狗解析（纯函数，不联网）
# --------------------------------------------------------------------------- #
def test_parse_sogou_links_and_titles():
    page = """
    <ul class="news-list">
      <li><div class="txt-box"><h3><a href="/link?url=abc123">标题一</a></h3></div></li>
      <li><div class="txt-box"><h3><a href='/link?url=def456'>标题二</a></h3></div></li>
    </ul>
    """
    links = parse_sogou_links(page)
    assert links == [
        "https://weixin.sogou.com/link?url=abc123",
        "https://weixin.sogou.com/link?url=def456",
    ]
    assert parse_sogou_titles(page) == ["标题一", "标题二"]


def test_decode_sogou_hop():
    hop = """
    <script>
      var url = '';
      url += 'https://mp.';
      url += 'weixin.qq.c';
      url += 'om/s?src=11&timestamp=179336984';
      url += '3&ver=7058&signature=abc';
      setTimeout(function(){ window.location.replace(url); }, 100);
    </script>
    """
    assert decode_sogou_hop(hop) == (
        "https://mp.weixin.qq.com/s?src=11&timestamp=1793369843&ver=7058&signature=abc"
    )
    assert decode_sogou_hop("") == ""


# --------------------------------------------------------------------------- #
# 日期区间
# --------------------------------------------------------------------------- #
def _crawl(base: str, monkeypatch, *, since=0, until=0, max_pages=5):
    import wxrc.getmsg as gm

    monkeypatch.setattr(gm, "PROFILE_URL", f"{base}/mp/profile_ext")
    engine = make_engine([Account(name="a1", uin="u1", key="k1", quota_hour=999)])
    crawler = ProfileCrawler(engine, cursor=Cursor(None), page_pause=(0.001, 0.002))
    return list(crawler.crawl("MZTESTBIZ==", limit=50, max_pages=max_pages, since=since, until=until))


def test_date_range_keeps_window(mock, monkeypatch):
    state, base = mock
    state.key_limit = 999
    since = BASE_TS + 3 * DAY
    until = BASE_TS + 6 * DAY

    items = _crawl(base, monkeypatch, since=since, until=until)

    assert [i.publish_ts for i in items] == [BASE_TS + k * DAY for k in range(3, 7)]
    assert [i.title for i in items] == [f"测试文章 {k}" for k in range(3, 7)]


def test_date_range_stops_after_crossing_lower_bound(mock, monkeypatch):
    """第 1 页就出现早于 since 的条目 → 立刻收，不再翻第 2 页。"""
    state, base = mock
    state.key_limit = 999
    since = BASE_TS + 2 * DAY

    items = _crawl(base, monkeypatch, since=since, max_pages=5)

    # mock 第 1 页是 i=0..7，边界在 i=2，之后都满足 since
    assert all(i.publish_ts >= since for i in items)
    assert len(items) == 6, "只应产出 i=2..7，不能翻到第二页"
    assert state.key_hits.get("k1", 0) == 1, "命中下界后必须停止翻页"


def test_no_range_returns_everything(mock, monkeypatch):
    state, base = mock
    state.key_limit = 999
    items = _crawl(base, monkeypatch)
    assert len(items) == 16


def test_archive_job_respects_date_range(mock, monkeypatch, tmp_path):
    """整条链路：biz + since/until → 只归档区间内的文章。"""
    import json

    from wxrc.crawl import ArchiveJob

    state, base = mock
    state.key_limit = 999
    import wxrc.getmsg as gm

    monkeypatch.setattr(gm, "PROFILE_URL", f"{base}/mp/profile_ext")
    engine = make_engine([Account(name="a1", uin="u1", key="k1", quota_hour=999)])
    job = ArchiveJob(engine, tmp_path, article_pause=(0.001, 0.002))

    stats = job.run(
        "MZTESTBIZ==",
        limit=50,
        max_pages=3,
        since=BASE_TS + 3 * DAY,
        until=BASE_TS + 5 * DAY,
    )

    assert stats.listed == 3
    recs = [json.loads(x) for x in (tmp_path / "index.jsonl").read_text("utf-8").splitlines()]
    assert [r["title"] for r in recs] == ["测试文章 3", "测试文章 4", "测试文章 5"]
    assert all(BASE_TS + 3 * DAY <= r["publish_ts"] <= BASE_TS + 5 * DAY for r in recs)


@pytest.mark.skipif(
    not __import__("os").environ.get("WXRC_LIVE"), reason="需要真实网络：WXRC_LIVE=1"
)
def test_live_resolve_known_account():
    from wxrc.resolve import resolve_account

    engine = make_engine()
    got = resolve_account(engine, "共青团中央", max_hits=3, logger=lambda _m: None)
    assert got is not None and got.biz.startswith("Mz") and got.name == "共青团中央"
