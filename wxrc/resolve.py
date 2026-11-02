"""公众号名 → `__biz` 的解析（无凭证通道）。

为什么需要它：`getmsg` 只认 `__biz`，但用户手上只有「公众号名字」。

实测可用的桥（2026-10-30）：
    名字 → 搜狗微信文章搜索（type=2）→ `/link?url=...` → 跳转页里的 JS 拼出
    `mp.weixin.qq.com/s?src=11&timestamp=...&signature=...` → 打开后页面里有
    `var biz = "Mz..."` 和 `var nickname = "公众号名"` → 用名字校验后返回 biz。

已知边界：
- 搜狗是独立风控（`antispider`），高频会被踢；每跳间隔 1~2s，一次最多看 5 条；
- 搜索是相关性排序、**不是按号聚合**，所以必须逐条打开比对 `nickname`，匹配不到就返回 None；
- 这只是拿 biz；要枚举全量历史仍然要 `getmsg` 的会话凭证（见 README §10）。
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from typing import Callable

from .article import parse_article
from .session import RiskEngine

SOGOU_SEARCH = "https://weixin.sogou.com/weixin"
SOGOU_REFERER = "https://weixin.sogou.com/"

_LINK_RE = re.compile(r"""href=["'](/link\?url=[^"']+)["']""")
_HOP_PART_RE = re.compile(r"url \+= '([^']*)'")
_TITLE_RE = re.compile(r'<h3>\s*<a[^>]*>(.*?)</a>', re.S)


@dataclass
class Resolved:
    name: str
    biz: str
    title: str = ""
    article_url: str = ""
    hits_checked: int = 0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


# --------------------------------------------------------------------------- #
# 纯解析（可离线单测）
# --------------------------------------------------------------------------- #
def parse_sogou_links(page: str, origin: str = "https://weixin.sogou.com") -> list[str]:
    """从搜索结果页取出 `/link?url=...` 绝对地址（origin 供 mock/镜像复用）。"""
    out = []
    for path in _LINK_RE.findall(page or ""):
        href = _html.unescape(path)
        out.append(href if href.startswith("http") else origin.rstrip("/") + href)
    return out


def parse_sogou_titles(page: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", t).strip() for t in _TITLE_RE.findall(page or "")]


def decode_sogou_hop(hop_html: str) -> str:
    """搜狗跳转页把目标地址拆成 `url += '...'` 多段，拼起来才是真链。"""
    parts = _HOP_PART_RE.findall(hop_html or "")
    return "".join(parts).strip()


def _norm(name: str) -> str:
    return re.sub(r"[\s\"'“”‘’《》]+", "", name or "").lower()


# --------------------------------------------------------------------------- #
def resolve_account(
    engine: RiskEngine,
    name: str,
    *,
    max_hits: int = 5,
    pause: tuple[float, float] = (1.2, 2.4),
    logger: Callable[[str], None] | None = None,
) -> Resolved | None:
    """名字 → biz；失败返回 None（不抛异常）。"""
    log = logger or engine.log
    target = _norm(name)
    if not target:
        return None

    try:
        page = engine.get(
            SOGOU_SEARCH,
            params={"type": "2", "query": f'"{name}"', "ie": "utf8"},
            referer=SOGOU_REFERER,
            expect="html",
            max_attempts=2,
        )
    except Exception as exc:
        log(f"[resolve] 搜索失败：{exc}")
        return None
    if not page.verdict.ok:
        log(f"[resolve] 搜索被拦：{page.verdict.reason}")
        return None

    links = parse_sogou_links(page.text)[:max_hits]
    log(f"[resolve] 搜索命中 {len(links)} 条，逐条校验公众号名")
    for i, link in enumerate(links, 1):
        try:
            hop = engine.get(link, referer=SOGOU_REFERER, expect="html", max_attempts=1)
            target_url = decode_sogou_hop(hop.text)
            if not target_url:
                continue
            art_page = engine.get(target_url, referer=link, expect="html", max_attempts=2)
            if not art_page.verdict.ok:
                continue
            art = parse_article(art_page.text, art_page.final_url)
        except Exception as exc:
            log(f"[resolve] 第 {i} 条解析失败：{exc}")
            continue

        if _norm(art.account) == target and art.biz:
            log(f"[resolve] 命中：{art.account} -> {art.biz}")
            return Resolved(
                name=art.account,
                biz=art.biz,
                title=art.title,
                article_url=art_page.final_url,
                hits_checked=i,
            )
        log(f"[resolve] 第 {i} 条是「{art.account or '?'}」，跳过")
        engine.sleep(engine.rng.uniform(*pause))

    return None


def biz_from_url(engine: RiskEngine, url: str) -> Resolved | None:
    """直接给一篇文章链接：从页面里取 biz + 账号名，最稳的一条路。"""
    page = engine.get(url, referer="https://mp.weixin.qq.com/", expect="html")
    if not page.verdict.ok:
        return None
    art = parse_article(page.text, page.final_url)
    if not art.biz:
        return None
    return Resolved(name=art.account, biz=art.biz, title=art.title, article_url=page.final_url, hits_checked=1)
