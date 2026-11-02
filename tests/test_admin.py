"""公众号后台（发表记录）路线的验证：解析、翻页、日期窗口、登录失效/限频。"""
from __future__ import annotations

import json
import random

import pytest

from wxrc.limiter import AdaptivePacer, CircuitBreaker
from wxrc.mp_admin import (
    AdminClient,
    AdminError,
    AdminSession,
    parse_published,
    parse_time,
)
from wxrc.proxy_pool import ProxyNode, ProxyPool
from wxrc.session import RiskEngine


def make_engine() -> RiskEngine:
    return RiskEngine(
        proxy_pool=ProxyPool(nodes=[ProxyNode(url="", label="direct")]),
        accounts=[],
        pacer=AdaptivePacer(base_interval=0.001, jitter_ratio=0.1),
        breaker=CircuitBreaker(threshold=30, open_seconds=10),
        bucket_rate=1000,
        max_attempts=2,
        sleep=lambda _s: None,
        logger=lambda _m: None,
        rng=random.Random(31),
    )


def session(token: str = "tok123") -> AdminSession:
    return AdminSession(cookie="slave_sid=abc; slave_user=gh_demo", token=token, user_agent="Mozilla/5.0")


# --------------------------------------------------------------------------- #
def test_parse_time_variants():
    assert parse_time("2025年06月28日 08:00") == parse_time("2025-06-28 08:00:00")
    assert parse_time("2025/6/8 7:32") > 0
    assert parse_time("") == 0


def test_parse_nested_publish_page():
    msg = {
        "title": "标题A",
        "content_url": "https://mp.weixin.qq.com/s/abc?__biz=B==&mid=1&idx=1&sn=x",
        "line_info": {"send_time": "2025-06-20 08:00"},
        "appmsgid": "1001",
        "itemidx": "2",
        "read_num": 4321,
        "like_num": 12,
        "old_like_num": 6,
        "comment_count": 3,
    }
    info = {"appmsg_info": [msg]}
    page = {"publish_list": [{"publish_info": json.dumps(info, ensure_ascii=False).replace('"', "&quot;")}], "total_count": 1}
    payload = {"base_resp": {"ret": 0}, "publish_page": json.dumps(page, ensure_ascii=False)}

    items, total = parse_published(json.dumps(payload, ensure_ascii=False))

    assert total == 1 and len(items) == 1
    it = items[0]
    assert it.title == "标题A" and it.read_num == 4321 and it.like_num == 12
    assert it.comment_count == 3 and it.itemidx == "2"
    assert it.publish_ts == parse_time("2025-06-20 08:00")
    assert "read_num" in it.raw_keys


def test_admin_lists_and_filters_by_date(mock):
    _state, base = mock
    import wxrc.mp_admin as mp

    engine = make_engine()
    client = AdminClient(engine, session())

    # 直接打 mock 端点
    old = mp.PUBLISH_URL
    mp.PUBLISH_URL = f"{base}/cgi-bin/appmsgpublish"
    try:
        items = list(client.iter_published(max_pages=5, pause=(0.001, 0.002)))
        assert len(items) == 25
        assert items[0].title == "后台文章 0" and items[0].read_num == 1000

        window = list(
            client.iter_published(
                max_pages=5,
                since=parse_time("2025-06-20"),
                until=parse_time("2025-06-22 23:59:59"),
                pause=(0.001, 0.002),
            )
        )
    finally:
        mp.PUBLISH_URL = old

    # 2025-06-28 起倒序：i=6→06-22, i=7→06-21, i=8→06-20
    assert [i.publish_text[:10] for i in window] == ["2025-06-22", "2025-06-21", "2025-06-20"]
    assert all(1006 <= i.read_num <= 1008 for i in window)


def test_admin_expired_login(mock):
    _state, base = mock
    import wxrc.mp_admin as mp

    old = mp.PUBLISH_URL
    mp.PUBLISH_URL = f"{base}/cgi-bin/appmsgpublish"
    try:
        client = AdminClient(make_engine(), session(token="wrong"))
        with pytest.raises(AdminError) as exc:
            list(client.iter_published(max_pages=2))
        assert exc.value.expired
    finally:
        mp.PUBLISH_URL = old


def test_admin_rate_limited(mock):
    state, base = mock
    state.admin_rate_limit = True
    import wxrc.mp_admin as mp

    old = mp.PUBLISH_URL
    mp.PUBLISH_URL = f"{base}/cgi-bin/appmsgpublish"
    try:
        client = AdminClient(make_engine(), session())
        with pytest.raises(AdminError) as exc:
            list(client.iter_published(max_pages=2))
        assert exc.value.rate_limited
    finally:
        mp.PUBLISH_URL = old


def test_parse_comments_page_js_literal():
    from wxrc.mp_admin import parse_comments_page

    html = """<script>
    var app_msg_list = {"app_msg":[
      {"item":{"title":"T1"},"content_url":"https://mp.weixin.qq.com/s/x","date_time":1750000000,
       "total_comment_cnt":2,
       "comment":{"total_count":2,"title":"T1","comment":[
         {"nick_name":"A","content":"&quot;hi&quot;","post_time":1750000000,"like_num":2,
          "is_elected":1,"reply":{"reply_list":[{"nick_name":"B","content":"re"}]}}]}}
    ]};
    </script>"""
    rows = parse_comments_page(html)
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "T1" and row["total"] == 2
    c = row["comments"][0]
    assert c["name"] == "A" and c["content"] == '"hi"' and c["like_num"] == 2 and c["elected"]
    assert c["replies"][0]["name"] == "B"


def test_js_to_json_handles_single_quotes():
    from wxrc.mp_admin import _js_to_json

    assert _js_to_json("{a: 'x', b: 1}") == '{"a": "x", "b": 1}'


def test_session_roundtrip(tmp_path):
    s = session()
    path = tmp_path / "admin.json"
    s.save(path)
    loaded = AdminSession.load(path)
    assert loaded.cookie == s.cookie and loaded.token == s.token and loaded.ok
