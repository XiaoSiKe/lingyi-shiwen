"""列表通道：`mp/profile_ext?action=getmsg`（微信客户端历史消息接口）。

这是能翻到「全量历史」的唯一非官方通道，也是风控最狠的一层：
- 凭证 30 分钟一过期（`ret=200003` 就是 key 没了）；
- 翻页有节奏限制（`ret=-3`），单号连续翻 5~10 页就会开始拦；
- `ret=-6` 是行为风控，说明这个号这几分钟不能再动。

所以这里的核心不是「怎么拼 URL」，而是：
游标持久化（断点续翻）+ 每页判定 ret + 命中就交给引擎换号 + 去重。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import parse_qs, quote, urlparse

from .account_pool import Account
from .guard import Risk
from .session import Exhausted, NoIdentity, Reply, RiskEngine
from .storage import atomic_write

PROFILE_URL = "https://mp.weixin.qq.com/mp/profile_ext"
PAGE_COUNT = 8


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class ArticleRef:
    title: str = ""
    url: str = ""
    digest: str = ""
    cover: str = ""
    publish_ts: int = 0
    author: str = ""
    item_idx: int = 0
    biz: str = ""
    mid: str = ""
    idx: str = ""
    sn: str = ""

    def key(self) -> str:
        if self.mid and self.idx:
            return f"{self.biz}:{self.mid}:{self.idx}"
        return self.url or self.title

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "digest": self.digest,
            "cover": self.cover,
            "publish_ts": self.publish_ts,
            "author": self.author,
            "biz": self.biz,
            "mid": self.mid,
            "idx": self.idx,
            "sn": self.sn,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ArticleRef":
        return cls(**{k: d.get(k, "") for k in cls.__dataclass_fields__ if k in d})


# --------------------------------------------------------------------------- #
def build_profile_url(biz: str, account: Account, offset: int, count: int = PAGE_COUNT) -> tuple[str, dict]:
    """返回 (base_url, params)。参数顺序与客户端一致，便于和抓包逐字对照。"""
    params = {
        "action": "getmsg",
        "__biz": biz,
        "f": "json",
        "offset": str(int(offset)),
        "count": str(int(count)),
        "is_ok": "1",
        "scene": "124",
        "uin": account.uin,
        "key": account.key,
        "pass_ticket": account.pass_ticket,
        "wxtoken": "",
        "appmsg_token": account.appmsg_token,
        "x5": "0",
    }
    return PROFILE_URL, {k: v for k, v in params.items() if v != ""}


def parse_getmsg(payload: dict) -> dict:
    """响应 -> {items, next_offset, can_continue, ret, errmsg}。"""
    out: dict = {"items": [], "next_offset": 0, "can_continue": False, "ret": 0, "errmsg": ""}
    if not isinstance(payload, dict):
        return out
    out["ret"] = _int(payload.get("ret"))
    out["errmsg"] = str(payload.get("errmsg") or "")
    out["next_offset"] = _int(payload.get("next_offset"))
    out["can_continue"] = bool(_int(payload.get("can_msg_continue")))

    raw = payload.get("general_msg_list") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raw = {}
    for msg in (raw.get("list") if isinstance(raw, dict) else []) or []:
        if not isinstance(msg, dict):
            continue
        comm = msg.get("comm_msg_info") or {}
        ts = _int(comm.get("datetime"))
        ext = msg.get("app_msg_ext_info") or {}
        if isinstance(ext, dict) and ext:
            head = _ref_from_ext(ext, ts)
            if head:
                out["items"].append(head)
            for sub in ext.get("multi_app_msg_item_list") or []:
                if isinstance(sub, dict):
                    child = _ref_from_ext(sub, ts)
                    if child:
                        out["items"].append(child)
    return out


def _ref_from_ext(ext: dict, ts: int) -> ArticleRef | None:
    url = normalize_url(str(ext.get("content_url") or "").replace("&amp;", "&"))
    title = str(ext.get("title") or "").strip()
    if not url and not title:
        return None
    q = parse_qs(urlparse(url).query) if url else {}
    return ArticleRef(
        title=title,
        url=url,
        digest=str(ext.get("digest") or "").strip(),
        cover=str(ext.get("cover") or "").strip(),
        publish_ts=ts,
        author=str(ext.get("author") or "").strip(),
        item_idx=_int(ext.get("itemidx")),
        biz=(q.get("__biz") or [""])[0],
        mid=(q.get("mid") or [""])[0],
        idx=(q.get("idx") or [""])[0],
        sn=(q.get("sn") or [""])[0],
    )


def normalize_url(url: str) -> str:
    """getmsg 里给的常是 http 链接：只把微信域名升级到 https，别的原样保留。"""
    url = url.strip()
    if not url:
        return ""
    if url.startswith("http://"):
        host = urlparse(url).netloc.lower()
        if host.endswith("weixin.qq.com"):
            url = "https://" + url[len("http://"):]
    return url.split("#")[0]


def extract_biz(url: str) -> str:
    q = parse_qs(urlparse(url).query)
    biz = (q.get("__biz") or [""])[0]
    if biz:
        return biz
    # 短链 /s/xxxx 里拿不到 biz，只能靠 getmsg 返回的链接回填
    return ""


# --------------------------------------------------------------------------- #
@dataclass
class Cursor:
    """断点：biz -> {offset, seen_keys}。落盘后中断可续。"""

    path: Path | None = None
    data: dict[str, dict] = field(default_factory=dict)

    def load(self) -> "Cursor":
        if self.path and self.path.exists():
            try:
                self.data = json.loads(self.path.read_text("utf-8"))
            except (ValueError, OSError):
                self.data = {}
        return self

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=1))

    def get(self, biz: str) -> dict:
        return self.data.setdefault(biz, {"offset": 0, "seen": [], "done": False})

    def seen(self, biz: str) -> set[str]:
        return set(self.get(biz).get("seen") or [])

    def update(self, biz: str, *, offset: int, new_keys: list[str], done: bool) -> None:
        st = self.get(biz)
        st["offset"] = int(offset)
        seen = list(st.get("seen") or [])
        seen.extend(new_keys)
        st["seen"] = seen[-5000:]
        st["done"] = bool(done)


class ProfileCrawler:
    """按 offset 翻页直到 can_msg_continue=0；每页都过一次风控判定。"""

    def __init__(
        self,
        engine: RiskEngine,
        *,
        count: int = PAGE_COUNT,
        page_pause: tuple[float, float] = (2.5, 6.0),
        cursor: Cursor | None = None,
        on_page: Callable[[int, int, list[ArticleRef]], None] | None = None,
    ):
        self.engine = engine
        self.count = count
        self.page_pause = page_pause
        self.cursor = cursor or Cursor()
        self.on_page = on_page

    def crawl(
        self,
        biz: str,
        *,
        limit: int = 200,
        max_pages: int = 50,
        resume: bool = True,
        since: int = 0,
        until: int = 0,
    ) -> Iterator[ArticleRef]:
        state = self.cursor.get(biz)
        scope = [since, until]
        # v1 游标没有待处理队列，无法证明已落盘；重新枚举，由归档成功索引去重。
        if not resume or state.get("schema_version") != 2 or state.get("scope") != scope:
            state.clear()
            state.update(offset=0, seen=[], done=False, pending=[], schema_version=2, scope=scope)
            self.cursor.save()
        seen = self.cursor.seen(biz)
        emitted = 0
        pages = 0

        while emitted < limit:
            # 下游处理完 yield 才确认；中断时 pending 保留，允许安全重放。
            while state.get("pending") and emitted < limit:
                raw = state["pending"][0]
                ref = ArticleRef.from_dict(raw)
                yield ref
                emitted += 1
                seen.add(ref.key())
                state["seen"] = list(dict.fromkeys([*state.get("seen", []), ref.key()]))[-5000:]
                state["pending"].pop(0)
                self.cursor.save()
            if emitted >= limit or state.get("done") or pages >= max_pages:
                return
            offset = int(state.get("offset") or 0)
            reply = self._fetch_page(biz, offset)
            if reply.verdict.risk is Risk.GONE:
                return
            if not reply.verdict.ok:
                raise Exhausted(reply.attempts, reply)
            parsed = parse_getmsg(reply.json())
            items = parsed["items"]
            new: list[ArticleRef] = []
            page_seen: set[str] = set()
            reached_old = False
            for ref in items:
                if since and ref.publish_ts and ref.publish_ts < since:
                    reached_old = True
                    continue
                if until and ref.publish_ts and ref.publish_ts > until:
                    continue
                if ref.key() in seen or ref.key() in page_seen:
                    continue
                page_seen.add(ref.key())
                new.append(ref)

            pages += 1
            state["offset"] = parsed["next_offset"] or offset + len(items)
            state["done"] = (not parsed["can_continue"]) or not items or reached_old
            state["pending"] = [ref.to_dict() for ref in new]
            self.cursor.save()
            if self.on_page:
                self.on_page(pages, len(new), new)
            if pages > 1:
                self.engine.sleep(self.engine.rng.uniform(*self.page_pause))

    # ------------------------------------------------------------------ #
    def _fetch_page(self, biz: str, offset: int, *, tries: int = 4) -> Reply:
        """同一页在多号之间试：命中 ret 就把号交回池子冷却，立刻换号重放。"""
        tried: set[str] = set()
        last: Reply | None = None
        for _ in range(tries):
            acc = self.engine.accounts.select(exclude=tried, biz=biz)
            if acc is None:
                raise NoIdentity("没有可用账号，无法请求 getmsg（需要 uin/key 凭证）")
            tried.add(acc.name)
            base, params = build_profile_url(biz, acc, offset, self.count)
            try:
                reply = self.engine.get(
                    base,
                    params=params,
                    referer="https://mp.weixin.qq.com/mp/profile_ext",
                    xhr=True,
                    expect="json",
                    account=acc,
                    biz=biz,
                    max_attempts=1,
                    raise_on_exhaust=False,
                )
            except Exhausted as exc:
                last = exc.last
                continue
            last = reply
            risk = reply.verdict.risk
            if risk is Risk.OK:
                return reply
            if risk is Risk.GONE:
                return reply
            self.engine.log(f"[getmsg] offset={offset} 号 {acc.name} 命中 {risk.value}，换号重放")
        if last is None:
            raise NoIdentity("getmsg 无可用账号")
        return last
