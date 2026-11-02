"""搜索枚举：给公众号名 + 日期区间，从搜索结果里把该号的文章捞出来。

这不是「全量历史」——搜索引擎只收录一部分，且按相关性而不是时间排序。
它的价值在于：**零凭证**也能拿到一批该号的文章正文，用来做小样本/抽检/选题。

链路：搜狗文章搜索（`type=2`，可翻页）→ `/link?url=` 跳转页 JS 解码 →
打开文章页 → 校验 `nickname` 是否等于目标名 → 按 `ct` 过滤日期 → 去重（biz:mid:idx）。

实测（2026-10-30，共青团中央）：
    page1 18 条链接，前 6 条命中 1 篇
    page2 12 条链接，前 6 条命中 6 篇（有重复，靠 biz:mid:idx 去重）
    page3 10 条链接，前 6 条命中 0 篇
命中文章的时间跨度很散（2017/2020/2022 混在一起），所以日期过滤必须逐篇解析后做。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

from .article import Article, parse_article
from .resolve import SOGOU_REFERER, decode_sogou_hop, parse_sogou_links
from .session import RiskEngine

SOGOU_SEARCH = "https://weixin.sogou.com/weixin"


def _norm(text: str) -> str:
    return re.sub(r"[\s\"'“”‘’《》]+", "", text or "").lower()


def account_matches(want: str, got: str, *, exact: bool = True) -> bool:
    w, g = _norm(want), _norm(got)
    if not w or not g:
        return False
    return w == g if exact else w in g


def _abs_link(origin: str, href: str) -> str:
    if href.startswith("http"):
        return href
    parsed = urlparse(origin)
    return f"{parsed.scheme}://{parsed.netloc}{href}"


@dataclass
class SearchHit:
    url: str
    title: str = ""
    account: str = ""
    publish_ts: int = 0
    biz: str = ""
    mid: str = ""
    idx: str = ""
    article: Article | None = field(default=None, repr=False)

    def key(self) -> str:
        return f"{self.biz}:{self.mid}:{self.idx}" if (self.mid and self.idx) else self.url

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "account": self.account,
            "publish_ts": self.publish_ts,
            "biz": self.biz,
            "mid": self.mid,
            "idx": self.idx,
        }


@dataclass
class SearchRun:
    hits: list[SearchHit] = field(default_factory=list)
    pages: int = 0
    links: int = 0
    opened: int = 0
    deduped: int = 0
    other_account: int = 0
    out_of_range: int = 0
    no_date: int = 0

    def summary(self) -> dict:
        return {
            "hits": len(self.hits),
            "pages": self.pages,
            "links": self.links,
            "opened": self.opened,
            "deduped": self.deduped,
            "other_account": self.other_account,
            "out_of_range": self.out_of_range,
            "no_date": self.no_date,
        }


def search_articles(
    engine: RiskEngine,
    name: str,
    *,
    since: int = 0,
    until: int = 0,
    max_pages: int = 6,
    per_page: int = 10,
    exact: bool = True,
    search_url: str = SOGOU_SEARCH,
    pause: tuple[float, float] = (1.5, 3.0),
    logger: Callable[[str], None] | None = None,
    on_hit: Callable[[SearchHit], None] | None = None,
    on_page: Callable[[int, int, int], None] | None = None,
) -> SearchRun:
    """按名字枚举搜索结果，返回落在日期区间内的命中。"""
    log = logger or engine.log
    run = SearchRun()
    if not _norm(name):
        return run

    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        try:
            resp = engine.get(
                search_url,
                params={"type": "2", "query": f'"{name}"', "page": str(page), "ie": "utf8"},
                referer=SOGOU_REFERER,
                expect="html",
                max_attempts=2,
            )
        except Exception as exc:
            log(f"[search] 第 {page} 页失败：{exc}")
            break
        if not resp.verdict.ok:
            log(f"[search] 第 {page} 页被拦：{resp.verdict.reason}")
            break

        origin = f"{urlparse(search_url).scheme}://{urlparse(search_url).netloc}"
        links = [_abs_link(search_url, h) for h in parse_sogou_links(resp.text, origin)][:per_page]
        if not links:
            log(f"[search] 第 {page} 页无结果，停止")
            break
        run.pages += 1
        run.links += len(links)

        page_hits = 0
        for link in links:
            try:
                hop = engine.get(link, referer=SOGOU_REFERER, expect="html", max_attempts=1)
                target = decode_sogou_hop(hop.text)
                if not target:
                    continue
                art_page = engine.get(target, referer=link, expect="html", max_attempts=2)
                if not art_page.verdict.ok:
                    continue
                art = parse_article(art_page.text, art_page.final_url)
            except Exception as exc:
                log(f"[search] 单条解析失败：{type(exc).__name__}")
                continue

            run.opened += 1
            hit = SearchHit(
                url=art_page.final_url,
                title=art.title,
                account=art.account,
                publish_ts=art.publish_ts,
                biz=art.biz,
                mid=art.mid,
                idx=art.idx,
                article=art,
            )
            if hit.key() in seen:
                run.deduped += 1
                continue
            seen.add(hit.key())

            if not account_matches(name, art.account, exact=exact):
                run.other_account += 1
                engine.sleep(engine.rng.uniform(0.6, 1.4))
                continue
            if not art.publish_ts:
                run.no_date += 1
                if since or until:
                    continue
            if since and art.publish_ts < since:
                run.out_of_range += 1
                continue
            if until and art.publish_ts > until:
                run.out_of_range += 1
                continue

            run.hits.append(hit)
            page_hits += 1
            if on_hit:
                on_hit(hit)
            engine.sleep(engine.rng.uniform(0.6, 1.4))

        if on_page:
            on_page(page, len(links), page_hits)
        log(f"[search] 第 {page} 页：{len(links)} 条链接，命中 {page_hits} 篇（累计 {len(run.hits)}）")
        engine.sleep(engine.rng.uniform(*pause))

    return run
