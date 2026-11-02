"""纯文本模式：给一串文章 URL，产出 Markdown / JSONL，不需要公众号凭证。

和 `crawl` 的区别：
- `crawl` 先过 getmsg 拿列表（要 uin/key），再逐篇抓正文；
- `text` 直接吃你手上的链接（转发来的、搜到的、别人给的清单），只抓正文。

正文来自文章页静态 HTML 的 `#js_content`，所以**不需要登录**；阅读数/留言才需要会话，
这里默认不碰（`--metrics` 打开才会调 `metrics`）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Iterable

from .article import Article, parse_article
from .session import BreakerOpen, NoIdentity, RiskEngine
from .storage import append_jsonl, atomic_write, stable_id

CST = timezone(timedelta(hours=8))


def _safe(text: str, limit: int = 60) -> str:
    text = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", text or "").strip(" ._")
    return (text[:limit] or "untitled")


def _day(art: Article) -> str:
    if art.publish_ts:
        return datetime.fromtimestamp(art.publish_ts, CST).strftime("%Y-%m-%d")
    return "undated"


@dataclass
class TextResult:
    url: str
    ok: bool = False
    title: str = ""
    account: str = ""
    publish: str = ""
    chars: int = 0
    images: int = 0
    path: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class TextRun:
    engine: RiskEngine
    out_dir: Path
    pause: tuple[float, float] = (1.5, 3.5)
    save_raw: bool = False
    results: list[TextResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.out_dir = Path(self.out_dir)
        (self.out_dir / "raw").mkdir(parents=True, exist_ok=True)
        self.index_path = self.out_dir / "index.jsonl"

    # ------------------------------------------------------------------ #
    def fetch(self, url: str) -> tuple[Article | None, str]:
        try:
            reply = self.engine.get(url, referer="https://mp.weixin.qq.com/", expect="html")
        except (BreakerOpen, NoIdentity):
            raise
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"
        if not reply.verdict.ok:
            return None, f"{reply.verdict.risk.value}: {reply.verdict.reason}"
        art = parse_article(reply.text, reply.final_url)
        if art.deleted or not (art.content_text or art.markdown):
            return None, "正文为空（可能是图片消息/视频/已删）"
        return art, ""

    def save(self, art: Article, url: str) -> TextResult:
        account = _safe(art.account or "unknown")
        day = _day(art)
        title = _safe(art.title)
        folder = self.out_dir / account
        folder.mkdir(parents=True, exist_ok=True)
        key = f"{art.biz}:{art.mid}:{art.idx}" if art.biz and art.mid else url
        stem = f"{day}_{title}_{stable_id(key)}"
        path = folder / f"{stem}.md"

        body = art.markdown or art.content_text
        md = "\n".join(
            [
                "---",
                f"title: {json.dumps(art.title, ensure_ascii=False)}",
                f"account: {json.dumps(art.account, ensure_ascii=False)}",
                f"author: {json.dumps(art.author, ensure_ascii=False)}",
                f"publish: {day}",
                f"source: {json.dumps(url, ensure_ascii=False)}",
                f"biz: {json.dumps(art.biz, ensure_ascii=False)}",
                "---",
                "",
                f"# {art.title}",
                "",
                f"> {art.account} · {day} · {len(art.content_text)} 字",
                "",
                body,
                "",
            ]
        )
        atomic_write(path, md)
        if self.save_raw and art.content_html:
            atomic_write(self.out_dir / "raw" / account / f"{stem}.html", art.content_html)

        rec = {
            "url": url,
            "title": art.title,
            "account": art.account,
            "publish": day,
            "publish_ts": art.publish_ts,
            "chars": len(art.content_text),
            "images": len(art.images),
            "biz": art.biz,
            "mid": art.mid,
            "idx": art.idx,
            "sn": art.sn,
            "path": str(path.relative_to(self.out_dir)),
            "fetched_at": datetime.now(CST).isoformat(timespec="seconds"),
        }
        append_jsonl(self.index_path, rec)
        return TextResult(
            url=url, ok=True, title=art.title, account=art.account, publish=day,
            chars=len(art.content_text), images=len(art.images), path=str(path),
        )

    # ------------------------------------------------------------------ #
    def run(self, urls: Iterable[str], on_item: Callable[[TextResult], None] | None = None) -> list[TextResult]:
        for url in urls:
            url = (url or "").strip()
            if not url or url.startswith("#"):
                continue
            art, error = self.fetch(url)
            if art is None:
                res = TextResult(url=url, error=error or "抓取失败")
            else:
                res = self.save(art, url)
            self.results.append(res)
            if on_item:
                on_item(res)
            self.engine.sleep(self.engine.rng.uniform(*self.pause))
        return self.results
