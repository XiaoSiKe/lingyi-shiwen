"""公众号后台路线：`cgi-bin/appmsgpublish`（发表记录）。

为什么这条路最贴合「名字 + 日期 → 全量正文 + 阅读/评论」：

    后台登录（管理员扫码一次）
      └─ /cgi-bin/appmsgpublish?sub=list&begin=...   ← 全量发表记录（群发 + 发布 + 转载）
           ├─ 标题 / 链接 / 发表时间 / appmsgid     ← 精确日期过滤
           ├─ read_num / like_num（若该版本接口返回）← 阅读、在看、点赞
           └─ 每篇的 mp 链接 → 正文（静态页，零凭证）

与前面几条路的差别：
- `getmsg`（客户端 key）拿的是「客户端能看到的历史」，且 30 分钟过期；
- `search` 拿的是「搜索引擎收录的」，抽样；
- 后台拿的是「这个号自己发布的全部」，唯一能保证全量且时间精确的通道，但**必须是这个号的管理员**。

实测边界（2026-10-30）：匿名访问 `appmsgpublish` / `cgi-bin/home` / `appmsg?action=list_ex`
全部返回 14977 B 的登录页——后台是纯登录态校验，没有可绕的旁路。

字段兼容性：不同版本后台返回结构不同（`publish_page` 可能是 JSON、字符串、HTML 实体编码的 JSON；
`publish_info` 也可能是 `&quot;` 编码的字符串）。这里统一做宽容解析，并把**实际出现过的字段名**记录下来，
方便第一次用真实响应校准（`--dump-raw`）。
"""
from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterator

from .session import RiskEngine
from .reporting import Coverage
from .storage import atomic_write

CST = timezone(timedelta(hours=8))
PUBLISH_URL = "https://mp.weixin.qq.com/cgi-bin/appmsgpublish"
PAGE_SIZE = 10

RET_RATE = 200013
RET_EXPIRED = 200003

# 后台常见时间文案：2025年04月16日 07:32 / 2025-04-16 07:32:00 / 2025/4/16
_TIME_RE = re.compile(
    r"(?P<y>\d{4})\s*[年\-/]\s*(?P<m>\d{1,2})\s*[月\-/]\s*(?P<d>\d{1,2})\s*日?"
    r"(?:[ T]*?(?P<H>\d{1,2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?)?"
)


def parse_time(text: str) -> int:
    m = _TIME_RE.search(text or "")
    if not m:
        return 0
    try:
        dt = datetime(
            int(m.group("y")), int(m.group("m")), int(m.group("d")),
            int(m.group("H") or 0), int(m.group("M") or 0), int(m.group("S") or 0),
            tzinfo=CST,
        )
    except ValueError:
        return 0
    return int(dt.timestamp())


@dataclass
class AdminItem:
    title: str = ""
    url: str = ""
    publish_ts: int = 0
    publish_text: str = ""
    appmsgid: str = ""
    itemidx: str = ""
    digest: str = ""
    cover: str = ""
    read_num: int = 0
    like_num: int = 0
    old_like_num: int = 0
    comment_count: int = 0
    raw_keys: list[str] = field(default_factory=list)

    def key(self) -> str:
        return f"{self.appmsgid}:{self.itemidx}" if self.appmsgid else (self.url or self.title)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["publish_date"] = (
            datetime.fromtimestamp(self.publish_ts, CST).strftime("%Y-%m-%d") if self.publish_ts else ""
        )
        return d


@dataclass
class AdminSession:
    cookie: str = ""
    token: str = ""
    user_agent: str = ""
    created_at: float = field(default_factory=time.time)

    @classmethod
    def load(cls, path: str | Path) -> "AdminSession":
        data = json.loads(Path(path).read_text("utf-8"))
        sess = cls(**{k: data.get(k, "") for k in ("cookie", "token", "user_agent")})
        try:
            sess.created_at = float(data.get("created_at") or sess.created_at)
        except (TypeError, ValueError):
            pass
        return sess

    def save(self, path: str | Path) -> None:
        atomic_write(Path(path),
            json.dumps(
                {
                    "cookie": self.cookie,
                    "token": self.token,
                    "user_agent": self.user_agent,
                    "created_at": self.created_at,
                },
                ensure_ascii=False,
                indent=1,
            ),
            private=True,
        )

    @property
    def ok(self) -> bool:
        return bool(self.cookie and self.token)


# --------------------------------------------------------------------------- #
# 解析层（宽容：JSON / var publish_page / HTML 实体编码 JSON / 双层字符串）
# --------------------------------------------------------------------------- #
def _as_dict(value: Any) -> dict:
    """字符串 -> dict。先按原样解析，失败再 unescape：

    顺序不能反——外层 JSON 里嵌着 `&quot;` 编码的内层 JSON，
    先 unescape 会把内层引号还原成真引号，直接破坏外层结构。
    """
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if not text:
        return {}
    for candidate in (text, html.unescape(text)):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}



def _extract_js_object(text: str, name: str) -> dict:
    """从整页 HTML/JS 里抠出 `name = {...}`（大括号配平 + 跳过字符串）。

    真实后台返回的不是纯 JSON，而是 102 KB 的页面，里面一行：
        publish_page = {"total_count":23,...};
    `publish_info` 又是 `&quot;` 编码的 JSON，所以不能靠正则整段匹配。
    """
    m = re.search(rf"\b{re.escape(name)}\s*=\s*", text or "")
    if not m:
        return {}
    start = text.find("{", m.end())
    if start < 0:
        return {}
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start : i + 1])
                except ValueError:
                    return {}
                return parsed if isinstance(parsed, dict) else {}
    return {}


def parse_publish_payload(text: str | bytes | dict) -> dict:
    if isinstance(text, dict):
        data = text
    else:
        raw = text.decode("utf-8", "ignore") if isinstance(text, bytes) else (text or "")
        data = _as_dict(raw)
        if not data:
            page = _extract_js_object(raw, "publish_page")
            data = {"publish_page": page} if page else {}
    page = data.get("publish_page") if isinstance(data, dict) else None
    if isinstance(page, str):
        page = _as_dict(page)
    if not isinstance(page, dict):
        page = data if isinstance(data, dict) and isinstance(data.get("publish_list"), list) else {}
    return page if isinstance(page, dict) else {}


def parse_published(payload: str | bytes | dict) -> tuple[list[AdminItem], int]:
    """返回 (条目, total_count)。"""
    page = parse_publish_payload(payload)
    total = 0
    try:
        total = int(page.get("total_count") or 0)
    except (TypeError, ValueError):
        total = 0

    items: list[AdminItem] = []
    for row in page.get("publish_list") or []:
        info = _as_dict(row.get("publish_info") if isinstance(row, dict) else row)
        for msg in info.get("appmsg_info") or []:
            if not isinstance(msg, dict):
                continue
            item = _item_from_msg(msg)
            if item:
                items.append(item)
    return items, total


def _item_from_msg(msg: dict) -> AdminItem | None:
    title = str(msg.get("title") or "").strip()
    url = str(msg.get("content_url") or msg.get("url") or "").strip()
    if not title and not url:
        return None

    line = msg.get("line_info")
    if isinstance(line, dict):
        raw_send = line.get("send_time")
    elif line is not None:
        raw_send = line
    else:
        raw_send = msg.get("send_time")

    # send_time 两种形态：unix 秒（新后台）或 "2025年06月28日 08:00"（旧后台）
    if isinstance(raw_send, (int, float)) or (
        isinstance(raw_send, str) and raw_send.isdigit() and len(raw_send) >= 9
    ):
        publish_ts = int(raw_send)
        send_text = datetime.fromtimestamp(publish_ts, CST).strftime("%Y-%m-%d %H:%M")
    else:
        send_text = str(raw_send or "").strip()
        publish_ts = parse_time(send_text)

    def num(*names: str) -> int:
        for name in names:
            value = msg.get(name)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return 0

    return AdminItem(
        title=title,
        url=url,
        publish_ts=publish_ts or num("create_time", "sent_time", "publish_time"),
        publish_text=send_text,
        appmsgid=str(msg.get("appmsgid") or msg.get("appmsg_id") or ""),
        itemidx=str(msg.get("itemidx") or msg.get("item_idx") or ""),
        digest=str(msg.get("digest") or "").strip(),
        cover=str(msg.get("cover") or msg.get("pic_cdn_url_1_1") or "").strip(),
        read_num=num("read_num", "readNum"),
        like_num=num("like_num", "likeNum"),
        old_like_num=num("old_like_num", "oldLikeNum", "like_old_num"),
        comment_count=num("comment_num", "comment_count", "commentNum"),
        raw_keys=sorted(msg.keys()),
    )


# --------------------------------------------------------------------------- #
class AdminClient:
    """后台发表记录客户端：一页 10 条，按 begin 翻页。"""

    def __init__(self, engine: RiskEngine, session: AdminSession, *, page_size: int = PAGE_SIZE):
        self.engine = engine
        self.session = session
        self.page_size = page_size
        self.report = Coverage("admin_records")

    def _headers(self) -> dict[str, str]:
        h = {
            "Cookie": self.session.cookie,
            "Referer": "https://mp.weixin.qq.com/",
            "X-Requested-With": "XMLHttpRequest",
        }
        if self.session.user_agent:
            h["User-Agent"] = self.session.user_agent
        return h

    def fetch_page(self, begin: int) -> tuple[str, int, int, list[AdminItem]]:
        """返回 (raw_text, ret, total)。"""
        params = {
            "sub": "list",
            "begin": str(begin),
            "count": str(self.page_size),
            "token": self.session.token,
            "lang": "zh_CN",
        }
        reply = self.engine.get(
            PUBLISH_URL,
            params=params,
            referer="https://mp.weixin.qq.com/",
            expect="html",          # 可能是 JSON，也可能是 JS 页面，统一按文本收
            want_account=False,
            max_attempts=2,
            raise_on_exhaust=False,
            extra_headers=self._headers(),
        )
        if not reply.verdict.ok:
            raise AdminError(f"后台请求失败：{reply.verdict.reason}", expired=reply.verdict.risk.value == "login")
        text = reply.text or ""
        if re.search(r"(?:扫码登录|登录公众号|微信公众平台.*登录)", text) and "publish_page" not in text:
            raise AdminError("后台登录失效，请重新扫码登录", expired=True)
        ret = 0
        try:
            data = json.loads(text)
            base = data.get("base_resp") if isinstance(data, dict) else None
            if isinstance(base, dict):
                ret = int(base.get("ret") or 0)
        except ValueError:
            ret = 0
        items, total = parse_published(text)
        return text, ret, total, items

    def latest_comments(self, *, begin: int = 0, count: int = 20) -> list[dict]:
        """后台「留言管理」最新留言（含未精选）。一页最多 20 条。"""
        reply = self.engine.get(
            "https://mp.weixin.qq.com/misc/appmsgcomment",
            params={
                "action": "list_latest_comment",
                "begin": str(begin),
                "count": str(count),
                "sendtype": "MASSSEND",
                "scene": "1",
                "token": self.session.token,
                "lang": "zh_CN",
            },
            referer="https://mp.weixin.qq.com/",
            expect="html",
            want_account=False,
            max_attempts=1,
            raise_on_exhaust=False,
            extra_headers=self._headers(),
        )
        return parse_comments_page(reply.text or "")

    def iter_published(self, **kwargs) -> Iterator[AdminItem]:
        self.report = Coverage("admin_records")
        try:
            yield from self._iter_published(**kwargs)
        except BaseException as exc:
            self.report.interrupt(exc)
            raise
        finally:
            if self.report.stop_reason == "running":
                self.report.stop("page_limit")

    def _iter_published(
        self,
        *,
        max_pages: int = 20,
        since: int = 0,
        until: int = 0,
        pause: tuple[float, float] = (2.0, 4.0),
    ) -> Iterator[AdminItem]:
        begin = 0
        seen: set[str] = set()
        for page in range(max_pages):
            text, ret, total, items = self.fetch_page(begin)
            self.report.pages += 1
            if ret == RET_EXPIRED:
                raise AdminError("后台登录失效 ret=200003，请重新扫码登录", expired=True)
            if ret == RET_RATE:
                raise AdminError("后台限频 ret=200013，等 10~30 分钟再试", rate_limited=True)
            if ret:
                raise AdminError(f"后台接口错误 ret={ret}")
            if not items:
                self.report.stop("empty_page")
                return
            new = 0
            for item in items:
                if item.key() in seen:
                    continue
                seen.add(item.key())
                if since and item.publish_ts and item.publish_ts < since:
                    self.report.stop("date_boundary", complete=True)
                    return
                if until and item.publish_ts and item.publish_ts > until:
                    continue
                new += 1
                yield item
            begin += self.page_size
            if total and begin >= total:
                self.report.stop("list_end", complete=True)
                return
            self.engine.sleep(self.engine.rng.uniform(*pause))


class AdminError(RuntimeError):
    def __init__(self, message: str, *, expired: bool = False, rate_limited: bool = False):
        super().__init__(message)
        self.expired = expired
        self.rate_limited = rate_limited


# --------------------------------------------------------------------------- #
# 登录：用标准浏览器打开后台，人工扫码一次，拿 cookie + token
# --------------------------------------------------------------------------- #
def login_via_browser(
    *, profile_key: str = "mp-admin", headless: bool = False,
    timeout_s: float = 240.0, save_path: str | Path | None = None,
    channel: str = "chromium", logger=None,
) -> AdminSession | None:
    """用户在标准浏览器中扫码，持久保存会话；始终关闭浏览器上下文。"""
    from .browser import playwright_available, profile_directory
    if not playwright_available():
        raise AdminError("请安装 requirements-browser.txt 并运行 python -m playwright install chromium")
    from playwright.sync_api import sync_playwright
    log = logger or (lambda _m: None)
    profile = profile_directory(profile_key)
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(profile), headless=headless, channel=None if channel == "chromium" else channel,
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://mp.weixin.qq.com/", wait_until="domcontentloaded", timeout=30000)
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                m = re.search(r"[?&]token=(\d+)", page.url)
                if m:
                    cookies = ctx.cookies(["https://mp.weixin.qq.com/"])
                    cookie = "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name"))
                    sess = AdminSession(cookie=cookie, token=m.group(1), user_agent=page.evaluate("navigator.userAgent"))
                    if sess.ok:
                        if save_path:
                            sess.save(save_path)
                        log("[admin] 已取得并保存后台会话")
                        return sess
                page.wait_for_timeout(1000)
            log("[admin] 等待扫码超时")
            return None
        finally:
            ctx.close()


# --------------------------------------------------------------------------- #
# 留言：/misc/appmsgcomment?action=list_latest_comment
# 页面里是 `var app_msg_list = {...}` 的 JS 字面量（key 不带引号、字符串可能是单引号），
# 不能直接 json.loads，先规范化再解析。
# --------------------------------------------------------------------------- #
_JS_KEY_RE = re.compile(r"([{,]\s*)([A-Za-z_$][\w$]*)\s*:")
_JS_STR_RE = re.compile(r"'((?:\\.|[^'\\])*)'")


def _js_to_json(js: str) -> str:
    quoted = _JS_KEY_RE.sub(r'\1"\2":', js or "")
    return _JS_STR_RE.sub(lambda m: json.dumps(m.group(1).replace("\\'", "'"), ensure_ascii=False), quoted)


def parse_comments_page(text: str) -> list[dict]:
    """页面 -> [{title, url, total, comments: [{name, content, ts, like_num, elected, replies}]}]"""
    block = _extract_js_object_raw(text, "app_msg_list")
    if not block:
        return []
    try:
        data = json.loads(_js_to_json(block))
    except ValueError:
        return []

    out: list[dict] = []
    for row in data.get("app_msg") or []:
        if not isinstance(row, dict):
            continue
        item = row.get("item") or {}
        box = row.get("comment") or {}
        if not isinstance(box, dict):
            continue
        comments = []
        for c in box.get("comment") or []:
            if not isinstance(c, dict):
                continue
            replies = []
            for r in ((c.get("reply") or {}).get("reply_list") or []):
                if isinstance(r, dict):
                    replies.append(
                        {
                            "name": str(r.get("nick_name") or ""),
                            "content": html.unescape(str(r.get("content") or "")),
                        }
                    )
            comments.append(
                {
                    "name": str(c.get("nick_name") or ""),
                    "content": html.unescape(str(c.get("content") or "")),
                    "ts": int(c.get("post_time") or 0),
                    "like_num": int(c.get("like_num") or 0),
                    "elected": bool(c.get("is_elected")),
                    "replies": replies,
                }
            )
        if not comments:
            continue
        out.append(
            {
                "title": str(item.get("title") or box.get("title") or ""),
                "url": str(row.get("content_url") or ""),
                "total": int(box.get("total_count") or row.get("total_comment_cnt") or len(comments)),
                "comments": comments,
            }
        )
    return out


def _extract_js_object_raw(text: str, name: str) -> str:
    """返回 `name = {...}` 的原始片段（不做 JSON 解析，容忍 JS 字面量）。"""
    m = re.search(rf"\b{re.escape(name)}\s*=\s*", text or "")
    if not m:
        return ""
    start = text.find("{", m.end())
    if start < 0:
        return ""
    depth = 0
    in_str = False
    quote = ""
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
            continue
        if ch in "\"'":
            in_str = True
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return ""
