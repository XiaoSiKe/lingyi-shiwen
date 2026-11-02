"""互动数据（阅读/点赞/留言）的验证。

真机边界：
- 无会话：`getappmsgext` 200 但无 `appmsgstat`；`appmsg_comment` 返回 `ret=-3 no session`；
- 有会话：`appmsgstat.read_num/like_num` + `elected_comment[]`。

这里用 mock 复刻这两种响应，并验证 ArchiveJob 会把数字写进 index.jsonl / md / comments/*.json。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from wxrc.account_pool import Account, AccountPool
from wxrc.crawl import ArchiveJob
from wxrc.limiter import AdaptivePacer, CircuitBreaker
from wxrc.metrics import MetricsClient, parse_appmsgstat, parse_comments
from wxrc.proxy_pool import ProxyNode, ProxyPool
from wxrc.session import RiskEngine

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "wechat-mp-collector"
    / "fixtures"
    / "article_ni3KysbNVM8LCcEGDB_1YA.html"
)


def make_engine(accounts: list[Account] | None = None) -> RiskEngine:
    return RiskEngine(
        proxy_pool=ProxyPool(nodes=[ProxyNode(url="", label="direct")]),
        account_pool=AccountPool(accounts or [], logger=lambda _m: None),
        pacer=AdaptivePacer(base_interval=0.001, jitter_ratio=0.1, max_factor=2.0),
        breaker=CircuitBreaker(threshold=50, open_seconds=10),
        bucket_rate=1000,
        max_attempts=3,
        sleep=lambda _s: None,
        logger=lambda _m: None,
        rng=random.Random(3),
    )


def make_client(base: str) -> MetricsClient:
    engine = make_engine([Account(name="a1", uin="u1", key="k1")])
    return MetricsClient(
        engine,
        stats_url=f"{base}/mp/getappmsgext",
        comment_url=f"{base}/mp/appmsg_comment",
    )


# --------------------------------------------------------------------------- #
def test_read_stats_with_session(mock):
    _state, base = mock
    client = make_client(base)
    engine = client.engine
    acc = engine.accounts.lease()
    stats = client.read_stats(
        biz="MZTESTBIZ==", mid="265750", idx="1", sn="sn0",
        account=acc, article_url=f"{base}/s/art0",
    )
    assert stats.ok and stats.read_num == 12345 and stats.like_num == 88
    assert stats.old_like_num == 77


def test_comments_with_session(mock):
    _state, base = mock
    client = make_client(base)
    engine = client.engine
    acc = engine.accounts.lease()
    items, total, reason = client.comments(
        biz="MZTESTBIZ==", idx="1", comment_id="3945457940655423488",
        account=acc, article_url=f"{base}/s/art0",
    )
    assert reason == "" and total == 2 and len(items) == 2
    assert items[0].name == "读者A" and items[0].like_num == 5
    assert items[0].replies[0]["content"] == "谢谢支持"
    assert items[0].create_time.startswith("2025-")


def test_no_session_degrades_gracefully(mock):
    _state, base = mock
    engine = make_engine([Account(name="a1", uin="u1", key="nokey")])
    client = MetricsClient(
        engine,
        stats_url=f"{base}/mp/getappmsgext",
        comment_url=f"{base}/mp/appmsg_comment",
    )
    acc = engine.accounts.lease()
    stats = client.read_stats(
        biz="MZTESTBIZ==", mid="265750", idx="1", sn="sn0",
        account=acc, article_url=f"{base}/s/art0",
    )
    assert not stats.ok and "appmsgstat" in stats.reason

    items, total, reason = client.comments(
        biz="MZTESTBIZ==", idx="1", comment_id="1", account=acc, article_url=f"{base}/s/art0",
    )
    assert items == [] and total == 0 and "no session" in reason


def test_parse_helpers():
    assert parse_appmsgstat({"base_resp": {"ret": 0}}).ok is False
    ok = parse_appmsgstat(
        {"base_resp": {"ret": 0}, "appmsgstat": {"read_num": "1.2万", "like_num": 3}}
    )
    assert ok.ok and ok.read_num == 0 and ok.like_num == 3  # 非法数字归零，不抛异常

    items, total, reason = parse_comments(
        {
            "ret": 0,
            "elected_comment_total_cnt": 1,
            "elected_comment": [{"content": "hi", "nick_name": "N", "like_num": "2"}],
        }
    )
    assert total == 1 and items[0].content == "hi" and items[0].like_num == 2 and reason == ""


@pytest.mark.skipif(not FIXTURE.exists(), reason="缺少真机 fixture")
def test_real_page_has_comment_id():
    from wxrc.article import parse_article

    art = parse_article(FIXTURE.read_text("utf-8", errors="replace"))
    assert art.comment_id == "3945457940655423488"
    assert art.biz == "MzA3NTE5MzQzMA==" and art.sn


def test_archive_job_writes_metrics(mock, tmp_path):
    state, base = mock
    state.key_limit = 100          # 列表接口不拦，专注 metrics
    engine = make_engine([Account(name="a1", uin="u1", key="k1", quota_hour=500)])
    job = ArchiveJob(
        engine, tmp_path,
        article_pause=(0.001, 0.002), with_metrics=True, comment_limit=20,
        metrics_urls=(f"{base}/mp/getappmsgext", f"{base}/mp/appmsg_comment"),
    )

    import wxrc.getmsg as getmsg_mod

    old = getmsg_mod.PROFILE_URL
    getmsg_mod.PROFILE_URL = f"{base}/mp/profile_ext"
    try:
        stats = job.run("MZTESTBIZ==", limit=2, max_pages=1)
    finally:
        getmsg_mod.PROFILE_URL = old

    assert stats.fetched == 2
    recs = [json.loads(x) for x in (tmp_path / "index.jsonl").read_text("utf-8").splitlines()]
    assert all(r["read_num"] == 12345 and r["like_num"] == 88 for r in recs)
    assert all(r["comment_count"] == 2 for r in recs)
    assert all(r["metrics_ok"] for r in recs)

    cdir = list(tmp_path.glob("**/comments/*.json"))
    assert len(cdir) == 2
    payload = json.loads(cdir[0].read_text("utf-8"))
    assert len(payload["comments"]) == 2 and payload["comments"][0]["name"] == "读者A"

    md = sorted(tmp_path.glob("**/*.md"))[0].read_text("utf-8")
    assert "阅读 12345" in md and "## 留言" in md and "谢谢支持" in md
