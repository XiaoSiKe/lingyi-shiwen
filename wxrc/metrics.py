"""互动数据层：阅读数 / 点赞 / 在看 / 留言。

实测边界（2026-10-30，真实文章页）：

| 数据 | 无会话（匿名 httpx / 匿名浏览器） | 有会话（uin + key + pass_ticket + cookie） |
|---|---|---|
| 正文文本 | ✅ 静态 HTML 里就有 `#js_content` | ✅ |
| 阅读 / 点赞 / 在看 | ❌ `getappmsgext` 返回 200 但**没有 `appmsgstat`**；页面里 `read_num = 0` | ✅ `appmsgstat.read_num/like_num` |
| 留言 | ❌ `appmsg_comment` 返回 `ret=-3 "no session"` | ✅ `elected_comment[]` + `elected_comment_total_cnt` |

所以本模块的所有请求都必须带账号。`fetch_all()` 在没有账号时直接返回 `reason`，不抛异常、不空转。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

from .account_pool import Account
from .guard import Risk
from .session import Exhausted, RiskEngine

CST = timezone(timedelta(hours=8))
STATS_URL = "https://mp.weixin.qq.com/mp/getappmsgext"
COMMENT_URL = "https://mp.weixin.qq.com/mp/appmsg_comment"


@dataclass
class Stats:
    read_num: int = 0
    like_num: int = 0
    old_like_num: int = 0
    comment_count: int = 0
    ok: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "read_num": self.read_num,
            "like_num": self.like_num,
            "old_like_num": self.old_like_num,
            "comment_count": self.comment_count,
            "ok": self.ok,
            "reason": self.reason,
        }


@dataclass
class Comment:
    name: str = ""
    content: str = ""
    like_num: int = 0
    create_time: str = ""
    replies: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "content": self.content,
            "like_num": self.like_num,
            "create_time": self.create_time,
            "replies": self.replies,
        }


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ts_text(ts: Any) -> str:
    value = _int(ts)
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value, CST).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- #
# 纯解析（可离线单测）
# --------------------------------------------------------------------------- #
def parse_appmsgstat(payload: dict | None) -> Stats:
    """`getappmsgext` 响应 -> Stats。

    有会话：`{"appmsgstat": {"read_num": N, ...}, "base_resp": {"ret": 0}}`
    无会话：同一接口 200 但**没有 appmsgstat**（实测），所以「字段缺失」本身就是要报的信号。
    """
    stats = Stats()
    if not isinstance(payload, dict):
        stats.reason = "响应不是 JSON"
        return stats
    base = payload.get("base_resp") or {}
    ret = _int(base.get("ret", payload.get("ret", 0)))
    if ret != 0:
        stats.reason = f"ret={ret} {base.get('errmsg') or payload.get('errmsg') or ''}".strip()
        return stats

    stat = payload.get("appmsgstat")
    if not isinstance(stat, dict) or not stat:
        stats.reason = "无 appmsgstat（无会话）"
        return stats

    stats.read_num = _int(stat.get("read_num"))
    stats.like_num = _int(stat.get("like_num"))
    stats.old_like_num = _int(stat.get("old_like_num"))
    stats.comment_count = _int(
        payload.get("comment_count")
        or stat.get("comment_count")
        or (payload.get("comment") or {}).get("count")
    )
    stats.ok = True
    return stats


def parse_comments(payload: dict | None) -> tuple[list[Comment], int, str]:
    """`appmsg_comment` 响应 -> (评论列表, 总数, 错误原因)。"""
    if not isinstance(payload, dict):
        return [], 0, "响应不是 JSON"
    base = payload.get("base_resp") or {}
    ret = _int(payload.get("ret", base.get("ret", 0)))
    if ret != 0:
        return [], 0, f"ret={ret} {payload.get('errmsg') or base.get('errmsg') or ''}".strip()

    total = _int(payload.get("elected_comment_total_cnt") or payload.get("comment_count"))
    out: list[Comment] = []
    for item in payload.get("elected_comment") or []:
        if not isinstance(item, dict):
            continue
        replies = []
        reply = item.get("reply") or {}
        if isinstance(reply, dict) and reply.get("content"):
            replies.append(
                {
                    "name": str(reply.get("nick_name") or ""),
                    "content": str(reply.get("content") or ""),
                }
            )
        out.append(
            Comment(
                name=str(item.get("nick_name") or ""),
                content=str(item.get("content") or ""),
                like_num=_int(item.get("like_num")),
                create_time=_ts_text(item.get("create_time")),
                replies=replies,
            )
        )
    return out, total or len(out), ""


# --------------------------------------------------------------------------- #
class MetricsClient:
    """带风控引擎的互动数据客户端：一次租号，读统计 + 拉留言。"""

    def __init__(
        self,
        engine: RiskEngine,
        *,
        referer: str = "https://mp.weixin.qq.com/",
        stats_url: str = STATS_URL,
        comment_url: str = COMMENT_URL,
    ):
        self.engine = engine
        self.referer = referer
        self.stats_url = stats_url
        self.comment_url = comment_url

    # ------------------------------------------------------------------ #
    def fetch_all(
        self,
        *,
        biz: str,
        mid: str,
        idx: str,
        sn: str,
        comment_id: str = "",
        article_url: str = "",
        comment_limit: int = 50,
        account: Account | None = None,
    ) -> dict:
        """返回 {"stats": Stats, "comments": [Comment], "total": int}，任何失败都降级为 reason。"""
        acc = account or self.engine.accounts.select(biz=biz)
        if acc is None:
            return {"stats": Stats(reason="无可用账号"), "comments": [], "total": 0}

        stats = self.read_stats(
            biz=biz, mid=mid, idx=idx, sn=sn, account=acc, article_url=article_url
        )
        comments: list[Comment] = []
        total = stats.comment_count
        if comment_id and not acc.dead and acc.available():
            comments, cnt, reason = self.comments(
                biz=biz,
                idx=idx,
                comment_id=comment_id,
                account=acc,
                article_url=article_url,
                limit=comment_limit,
            )
            if cnt:
                total = cnt
            if reason and not stats.reason:
                stats.reason = reason
        return {"stats": stats, "comments": comments, "total": total}

    # ------------------------------------------------------------------ #
    def read_stats(
        self,
        *,
        biz: str,
        mid: str,
        idx: str,
        sn: str,
        account: Account,
        article_url: str = "",
        retries: int = 2,
    ) -> Stats:
        params = {
            "f": "json",
            "__biz": biz,
            "mid": mid,
            "idx": idx or "1",
            "sn": sn,
            "key": account.key,
            "uin": account.uin,
            "pass_ticket": account.pass_ticket,
            "appmsg_token": account.appmsg_token,
            "wxtoken": "",
            "x5": "0",
        }
        params = {k: v for k, v in params.items() if v != ""}
        payload = self._call(self.stats_url, params, account, article_url, retries=retries, xhr=True)
        stats = parse_appmsgstat(payload)
        if not stats.ok and "appmsgstat" in stats.reason:
            # 客户端实际用 POST 提交表单；GET 拿不到时补一次 POST
            payload = self._call(
                self.stats_url,
                params,
                account,
                article_url,
                retries=1,
                xhr=True,
                method="POST",
                data=params,
            )
            stats = parse_appmsgstat(payload)
        return stats

    def comments(
        self,
        *,
        biz: str,
        idx: str,
        comment_id: str,
        account: Account,
        article_url: str = "",
        offset: int = 0,
        limit: int = 50,
        retries: int = 2,
    ) -> tuple[list[Comment], int, str]:
        params = {
            "action": "getcomment",
            "f": "json",
            "__biz": biz,
            "idx": idx or "1",
            "comment_id": comment_id,
            "offset": str(offset),
            "limit": str(limit),
            "uin": account.uin,
            "key": account.key,
            "pass_ticket": account.pass_ticket,
            "wxtoken": "777",
            "x5": "0",
        }
        params = {k: v for k, v in params.items() if v != ""}
        payload = self._call(self.comment_url, params, account, article_url, retries=retries, xhr=True)
        items, total, reason = parse_comments(payload)
        return items, total, reason

    # ------------------------------------------------------------------ #
    def _call(
        self,
        url: str,
        params: dict,
        account: Account,
        article_url: str,
        *,
        retries: int,
        xhr: bool,
        method: str = "GET",
        data: Any = None,
    ) -> dict | None:
        try:
            reply = self.engine.get(
                url,
                params=params if method == "GET" else None,
                method=method,
                data=data,
                referer=article_url or self.referer,
                xhr=xhr,
                expect="json",
                account=account,
                max_attempts=retries,
                raise_on_exhaust=False,
            )
        except Exhausted:
            return None
        if reply.payload is None:
            return None
        return reply.payload
