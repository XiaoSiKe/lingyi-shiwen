"""零一拾文归档编排：列表待处理队列 → 正文 → 文件与索引。

成功归档和确认删除进入完成索引；meta_only 独立保留，下次运行优先补采。
游标确认发生在下游持久化之后，正文与资源通过稳定标识避免同名覆盖。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

from .article import Article, parse_article
from .getmsg import ArticleRef, Cursor, ProfileCrawler
from .guard import Risk
from .metrics import MetricsClient
from .session import BreakerOpen, NoIdentity, RiskEngine
from .storage import append_jsonl, atomic_write, stable_id

CST = timezone(timedelta(hours=8))


def _safe_name(text: str, limit: int = 60) -> str:
    text = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", text or "").strip(" ._")
    return (text[:limit] or "untitled")


def _fmt_time(ts: int) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d")


@dataclass
class ArchiveStats:
    listed: int = 0
    fetched: int = 0
    skipped: int = 0
    failed: int = 0
    deleted: int = 0
    started: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            **self.__dict__,
            "elapsed": round(time.time() - self.started, 1),
        }


@dataclass
class ArchiveJob:
    engine: RiskEngine
    out_dir: Path
    article_pause: tuple[float, float] = (1.8, 4.5)
    download_images: bool = False
    with_metrics: bool = False       # 抓阅读/点赞/留言（需要账号会话）
    comment_limit: int = 50
    metrics_urls: tuple[str, str] | None = None   # (stats_url, comment_url)，默认线上

    def __post_init__(self) -> None:
        self.out_dir = Path(self.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.out_dir / "index.jsonl"
        self._records: dict[str, dict] = {}
        self._known = self._load_index()
        if self.with_metrics:
            urls = self.metrics_urls or ()
            kwargs = {"stats_url": urls[0], "comment_url": urls[1]} if len(urls) == 2 else {}
            self.metrics_client = MetricsClient(self.engine, **kwargs)
        else:
            self.metrics_client = None

    # ------------------------------------------------------------------ #
    def _load_index(self) -> set[str]:
        if self.index_path.exists():
            for line in self.index_path.read_text("utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                key = rec.get("key") or rec.get("url")
                if key:
                    self._records[key] = rec
        return {key for key, rec in self._records.items() if rec.get("status") in ("ok", "deleted")}

    def run(
        self,
        biz: str,
        *,
        limit: int = 100,
        max_pages: int = 50,
        resume: bool = True,
        since: int = 0,
        until: int = 0,
    ) -> ArchiveStats:
        stats = ArchiveStats()
        # 列表游标可能已结束，失败正文仍须独立补采。
        for rec in list(self._records.values()):
            if stats.listed >= limit:
                return stats
            if rec.get("status") != "meta_only" or rec.get("biz") not in ("", biz):
                continue
            ts = rec.get("publish_ts") or 0
            if (since and ts and ts < since) or (until and ts and ts > until):
                continue
            ref = ArticleRef.from_dict(rec)
            stats.listed += 1
            art = self.fetch_article(ref, stats)
            metrics = self._collect_metrics(art, ref) if art and not art.deleted and self.metrics_client else None
            self._save(ref, art, stats, metrics)
        if stats.listed >= limit:
            return stats
        cursor = Cursor(self.out_dir / "cursor.json").load()
        crawler = ProfileCrawler(
            self.engine,
            cursor=cursor,
            on_page=lambda page, new, items: self.engine.log(
                f"[list] 第 {page} 页 +{new} 条（累计 {stats.listed + new}）"
            ),
        )
        for ref in crawler.crawl(
            biz, limit=limit - stats.listed, max_pages=max_pages, resume=resume, since=since, until=until
        ):
            stats.listed += 1
            if ref.key() in self._known:
                stats.skipped += 1
                continue
            art = self.fetch_article(ref, stats)
            metrics = self._collect_metrics(art, ref) if art and not art.deleted and self.metrics_client else None
            self._save(ref, art, stats, metrics)
            self.engine.sleep(self.engine.rng.uniform(*self.article_pause))
        return stats

    # ------------------------------------------------------------------ #
    def fetch_article(self, ref: ArticleRef, stats: ArchiveStats) -> Article | None:
        if not ref.url:
            return None
        try:
            reply = self.engine.get(
                ref.url,
                referer="https://mp.weixin.qq.com/",
                expect="html",
            )
        except (BreakerOpen, NoIdentity):
            raise
        except Exception as exc:
            stats.failed += 1
            self.engine.log(f"[article] 放弃 {ref.url[:80]}：{exc}")
            return None
        if reply.verdict.risk is Risk.GONE:
            stats.deleted += 1
            return Article(url=ref.url, title=ref.title, deleted=True)
        if not reply.verdict.ok:
            stats.failed += 1
            return None
        art = parse_article(reply.text, ref.url)
        art.title = art.title or ref.title
        art.publish_ts = art.publish_ts or ref.publish_ts
        if art.deleted or not (art.markdown or art.content_text):
            stats.failed += 1
            self.engine.log(f"[article] 正文为空：{ref.url[:80]}")
            return None
        stats.fetched += 1
        return art

    # ------------------------------------------------------------------ #
    def _collect_metrics(self, art: Article, ref: ArticleRef) -> dict:
        """阅读/点赞/留言：失败也返回 dict，不让主流程断。"""
        assert self.metrics_client is not None
        biz = art.biz or ref.biz
        mid = art.mid or ref.mid
        idx = art.idx or ref.idx
        sn = art.sn or ref.sn
        if not (biz and mid and sn):
            return {"ok": False, "reason": "缺少 biz/mid/sn，无法查互动数据", "comments": []}
        try:
            got = self.metrics_client.fetch_all(
                biz=biz,
                mid=mid,
                idx=idx,
                sn=sn,
                comment_id=art.comment_id,
                article_url=ref.url,
                comment_limit=self.comment_limit,
            )
        except Exception as exc:
            self.engine.log(f"[metrics] 放弃 {ref.url[:60]}：{exc}")
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "comments": []}
        stats = got["stats"]
        self.engine.log(
            f"[metrics] {ref.title[:24]} 阅读={stats.read_num} 赞={stats.like_num} "
            f"留言={len(got['comments'])}/{got['total']} {stats.reason if not stats.ok else ''}"
        )
        return {
            "ok": stats.ok,
            "reason": stats.reason,
            "read_num": stats.read_num,
            "like_num": stats.like_num,
            "old_like_num": stats.old_like_num,
            "comment_count": got["total"] or stats.comment_count,
            "comments": [c.to_dict() for c in got["comments"]],
        }

    # ------------------------------------------------------------------ #
    def _save(
        self,
        ref: ArticleRef,
        art: Article | None,
        stats: ArchiveStats,
        metrics: dict | None = None,
    ) -> None:
        account = _safe_name((art.account if art else "") or "unknown")
        day = _fmt_time((art.publish_ts if art else 0) or ref.publish_ts) or "undated"
        month_dir = self.out_dir / account / day[:7]
        month_dir.mkdir(parents=True, exist_ok=True)
        title = _safe_name((art.title if art else "") or ref.title)
        stem = f"{day}_{title}_{stable_id(ref.key())}"
        metrics = metrics or {}

        rec = {
            "key": ref.key(),
            "url": ref.url,
            "title": (art.title if art else "") or ref.title,
            "account": account,
            "author": (art.author if art else "") or ref.author,
            "publish_ts": (art.publish_ts if art else 0) or ref.publish_ts,
            "publish_date": day,
            "digest": (art.digest if art else "") or ref.digest,
            "cover": (art.cover if art else "") or ref.cover,
            "biz": ref.biz,
            "mid": ref.mid,
            "idx": ref.idx,
            "sn": ref.sn,
            "images": len(art.images) if art else 0,
            "read_num": int(metrics.get("read_num") or 0),
            "like_num": int(metrics.get("like_num") or 0),
            "old_like_num": int(metrics.get("old_like_num") or 0),
            "comment_count": int(metrics.get("comment_count") or 0),
            "metrics_ok": bool(metrics.get("ok")),
            "metrics_reason": metrics.get("reason") or ("" if metrics.get("ok") else "未取得互动数据"),
            "deleted": bool(art.deleted) if art else False,
            "status": "ok" if art and not art.deleted else ("deleted" if art else "meta_only"),
            "fetched_at": datetime.now(CST).isoformat(timespec="seconds"),
        }

        if art and (art.markdown or art.content_text):
            if self.download_images and art.images:
                mapping = download_images(art, month_dir / "images", referer=ref.url or "")
                for remote, local in mapping.items():
                    art.markdown = art.markdown.replace(remote, f"images/{local}")
            md = self._render_md(rec, art, metrics.get("comments") or [])
            rec["path"] = str((month_dir / f"{stem}.md").relative_to(self.out_dir))
            atomic_write(month_dir / f"{stem}.md", md)
            raw = self.out_dir / account / day[:7] / "raw"
            raw.mkdir(parents=True, exist_ok=True)
            atomic_write(raw / f"{stem}.html", art.content_html or "")

        comments = metrics.get("comments") or []
        if comments:
            cdir = month_dir / "comments"
            cdir.mkdir(parents=True, exist_ok=True)
            atomic_write(cdir / f"{stem}.json",
                json.dumps(
                    {"url": ref.url, "total": rec["comment_count"], "comments": comments},
                    ensure_ascii=False,
                    indent=1,
                ),
            )

        append_jsonl(self.index_path, rec)
        self._records[rec["key"]] = rec
        if rec["status"] in ("ok", "deleted"):
            self._known.add(rec["key"])

    def _render_md(self, rec: dict, art: Article, comments: list[dict] | None = None) -> str:
        head = [
            "---",
            f"title: {json.dumps(rec['title'], ensure_ascii=False)}",
            f"account: {json.dumps(rec['account'], ensure_ascii=False)}",
            f"author: {json.dumps(rec['author'], ensure_ascii=False)}",
            f"publish: {rec['publish_date']}",
            f"source: {json.dumps(rec['url'], ensure_ascii=False)}",
            f"biz: {json.dumps(rec['biz'], ensure_ascii=False)}",
            f"read_num: {rec['read_num']}",
            f"like_num: {rec['like_num']}",
            f"comment_count: {rec['comment_count']}",
            f"metrics_ok: {str(rec['metrics_ok']).lower()}",
            f"metrics_reason: {json.dumps(rec['metrics_reason'], ensure_ascii=False)}",
            "---",
            "",
            f"# {rec['title']}",
            "",
            (f"> {rec['account']} · {rec['publish_date']} · 阅读 {rec['read_num']} · "
             f"赞 {rec['like_num']} · 留言 {rec['comment_count']}" if rec['metrics_ok'] else
             f"> {rec['account']} · {rec['publish_date']} · 互动数据未取得"),
            "",
        ]
        body = art.markdown or art.content_text
        tail = ""
        if comments:
            lines = ["", "---", "", "## 留言", ""]
            for c in comments[:50]:
                like = f"（{c.get('like_num')} 赞）" if c.get("like_num") else ""
                lines.append(f"- **{c.get('name') or '匿名'}**{like}：{c.get('content')}")
                for r in c.get("replies") or []:
                    lines.append(f"  - 回复 **{r.get('name') or ''}**：{r.get('content')}")
            tail = "\n".join(lines) + "\n"
        return "\n".join(head) + body + "\n" + tail


# --------------------------------------------------------------------------- #
def download_images(article: Article, dest: Path, *, referer: str = "https://mp.weixin.qq.com/") -> dict[str, str]:
    """mmbiz.qpic.cn 只认 Referer，补上就能下；返回 {原链: 本地相对路径}。"""
    dest.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str] = {}
    with httpx.Client(timeout=30, follow_redirects=True, trust_env=False) as client:
        for i, url in enumerate(article.images):
            try:
                resp = client.get(url, headers={"Referer": referer, "User-Agent": "Mozilla/5.0"})
                if resp.status_code != 200 or not resp.content:
                    continue
                ext = ".jpg"
                ct = resp.headers.get("content-type", "")
                if "png" in ct:
                    ext = ".png"
                elif "gif" in ct:
                    ext = ".gif"
                elif "webp" in ct:
                    ext = ".webp"
                name = f"{stable_id(url)}{ext}"
                atomic_write(dest / name, resp.content)
                mapping[url] = name
            except Exception:
                continue
    return mapping
