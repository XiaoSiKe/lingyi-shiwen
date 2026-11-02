"""搜索枚举的验证（离线，走本地 mock 的 /weixin + /link）。"""
from __future__ import annotations

import random

from wxrc.limiter import AdaptivePacer, CircuitBreaker
from wxrc.proxy_pool import ProxyNode, ProxyPool
from wxrc.search import account_matches, search_articles
from wxrc.session import RiskEngine

BASE_TS = 1750000000
DAY = 86400


def make_engine() -> RiskEngine:
    return RiskEngine(
        proxy_pool=ProxyPool(nodes=[ProxyNode(url="", label="direct")]),
        accounts=[],
        pacer=AdaptivePacer(base_interval=0.001, jitter_ratio=0.1),
        breaker=CircuitBreaker(threshold=30, open_seconds=10),
        bucket_rate=1000,
        max_attempts=3,
        sleep=lambda _s: None,
        logger=lambda _m: None,
        rng=random.Random(21),
    )


def test_account_matches():
    assert account_matches("共青团中央", "共青团中央")
    assert not account_matches("共青团中央", "共青团中央学校部")
    assert account_matches("共青团", "共青团中央学校部", exact=False)
    assert not account_matches("共青团中央", "别的公众号")


def test_search_filters_account_and_date(mock):
    state, base = mock
    state.ua_limit = 50
    run = search_articles(
        make_engine(),
        "测试公众号",
        since=BASE_TS + 2 * DAY,
        until=BASE_TS + 4 * DAY,
        search_url=f"{base}/weixin",
        max_pages=2,
        per_page=10,
        pause=(0.001, 0.002),
    )

    # mock 第 1 页给了：艺术号 art3（在区间）+ 别家 + 重复 art3
    assert len(run.hits) == 1
    hit = run.hits[0]
    assert hit.account == "测试公众号"
    assert hit.publish_ts == BASE_TS + 3 * DAY
    assert hit.title == "测试文章 3"
    assert run.deduped >= 1, "重复链接必须去掉"
    assert run.other_account >= 1, "别家的文章不能混进来"
    assert run.pages == 1, "第 2 页无结果应停止"


def test_search_date_window_excludes(mock):
    _state, base = mock
    run = search_articles(
        make_engine(),
        "测试公众号",
        since=BASE_TS + 10 * DAY,
        search_url=f"{base}/weixin",
        max_pages=1,
        per_page=10,
        pause=(0.001, 0.002),
    )
    assert run.hits == []
    assert run.out_of_range >= 1
