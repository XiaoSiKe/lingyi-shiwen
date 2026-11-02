"""CLI：`python -m wxrc <crawl|parse|snapshot>`。

配置示例见 config.example.json；凭证放 accounts.json（一行一个号）。
"""
from __future__ import annotations

from contextlib import nullcontext
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .account_pool import Account, AccountPool
from .article import parse_article
from .crawl import ArchiveJob
from .getmsg import Cursor, ProfileCrawler
from .limiter import AdaptivePacer, CircuitBreaker
from .proxy_pool import ProxyNode, ProxyPool
from .session import BreakerOpen, NoIdentity, RiskEngine, RiskEngineError
from .storage import output_lock, atomic_write
from .security import redact
from .mp_admin import AdminError

CST = timezone(timedelta(hours=8))
_ACTIVE_ENGINES: list[RiskEngine] = []


def _track_engine(engine: RiskEngine) -> RiskEngine:
    _ACTIVE_ENGINES.append(engine)
    return engine


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text("utf-8"))


def build_engine(cfg: dict, *, logger=print) -> RiskEngine:
    net = cfg.get("net", {})
    if net.get("profile_url"):
        # 允许指向镜像/本地 mock（默认走线上 mp.weixin.qq.com）
        from . import getmsg

        getmsg.PROFILE_URL = str(net["profile_url"])
    if net.get("publish_url"):
        from . import mp_admin

        mp_admin.PUBLISH_URL = str(net["publish_url"])
    accounts = [
        Account(
            name=a.get("name") or f"acc{i}",
            uin=str(a.get("uin") or ""),
            key=str(a.get("key") or ""),
            pass_ticket=str(a.get("pass_ticket") or ""),
            appmsg_token=str(a.get("appmsg_token") or ""),
            wap_sid2=str(a.get("wap_sid2") or ""),
            biz=str(a.get("biz") or ""),
            quota_hour=int(a.get("quota_hour") or 60),
            quota_day=int(a.get("quota_day") or 400),
            client=str(a.get("client") or "ios"),
            device_seed=int(a.get("device_seed") or 0),
            rpm=int(a.get("rpm") or 12),
        )
        for i, a in enumerate(cfg.get("accounts") or [])
    ]
    proxies = cfg.get("proxies") or [""]
    bcfg = cfg.get("browser") or {}
    hook = None
    if str(bcfg.get("engine", "off")).lower() not in ("off", "", "none"):
        from .browser import make_browser_hook

        hook = make_browser_hook(
            bcfg.get("engine", "auto"),
            logger=logger,
            data_root=bcfg.get("data_root") or None,
            headless=bool(bcfg.get("headless", True)),
            chrome=str(bcfg.get("chrome") or ""),
            profile_key=str((cfg.get("target") or {}).get("biz") or ""),
            locale=str(bcfg.get("locale") or "zh-CN"),
            wait_human_ms=int(bcfg.get("wait_human_ms", 90_000)),
        )
        logger(f"[browser] engine={bcfg.get('engine')} hook={'ok' if hook else '未启用'}")
    return _track_engine(RiskEngine(
        browser_hook=hook,
        proxy_pool=ProxyPool(nodes=[ProxyNode(url=u, label=(u or "direct")) for u in proxies]),
        account_pool=AccountPool(accounts, logger=logger),
        pacer=AdaptivePacer(
            base_interval=float(net.get("base_interval", 3.2)),
            jitter_ratio=float(net.get("jitter_ratio", 0.55)),
            max_factor=float(net.get("max_factor", 12.0)),
        ),
        breaker=CircuitBreaker(
            threshold=int(net.get("breaker_threshold", 5)),
            open_seconds=float(net.get("breaker_seconds", 600)),
        ),
        cool=cfg.get("cool") or {},
        timeout=float(net.get("timeout", 20)),
        max_attempts=int(net.get("max_attempts", 8)),
        verify=bool(net.get("verify", True)),
        logger=logger,
    ))


def cmd_crawl(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    target = cfg.get("target", {})
    biz = args.biz or target.get("biz") or ""
    engine = build_engine(cfg)

    # 没有 biz 时按 --from-url / --name 解析
    if not biz and getattr(args, "from_url", ""):
        from .resolve import biz_from_url

        got = biz_from_url(engine, args.from_url)
        if got:
            biz = got.biz
            print(f"[resolve] 从文章解析：{got.name} -> {biz}")
    if not biz and getattr(args, "name", ""):
        from .resolve import resolve_account

        got = resolve_account(engine, args.name, logger=print)
        if got:
            biz = got.biz
            print(f"[resolve] 名字解析：{got.name} -> {biz}")
    if not biz:
        print("缺少 __biz：用 --biz / --from-url / --name 或 config.target.biz")
        return 2

    since = _to_ts(getattr(args, "since", ""), end=False)
    until = _to_ts(getattr(args, "until", ""), end=True)
    if args.dry_run:
        crawler = ProfileCrawler(engine, cursor=Cursor(None))
        n = 0
        try:
            for ref in crawler.crawl(
                biz, limit=args.limit, max_pages=args.max_pages, since=since, until=until
            ):
                print(f"{ref.publish_ts}\t{ref.title}\t{ref.url}")
                n += 1
        except (NoIdentity, BreakerOpen) as exc:
            print(f"列表接口需要会话凭证（uin/key/pass_ticket）：{exc}")
            return 3
        print(f"共 {n} 条（dry-run，未抓正文）")
        print(json.dumps(crawler.report.as_dict(), ensure_ascii=False))
        return 0

    job = ArchiveJob(
        engine,
        Path(args.out),
        with_metrics=bool((cfg.get("metrics") or {}).get("enabled", True)),
        comment_limit=int((cfg.get("metrics") or {}).get("comment_limit", 50)),
    )
    try:
        stats = job.run(
            biz,
            limit=args.limit,
            max_pages=args.max_pages,
            resume=not args.no_resume,
            since=since,
            until=until,
            update=args.update,
        )
    except (NoIdentity, BreakerOpen) as exc:
        print(f"列表接口需要会话凭证（uin/key/pass_ticket）：{exc}")
        return 3
    print(json.dumps({"stats": stats.as_dict(), "engine": engine.snapshot()}, ensure_ascii=False, indent=1))
    return 1 if stats.failed else 0


def _to_ts(text: str, *, end: bool) -> int:
    """'2025-01-01' / '2025-01-01 08:00' → 东八区时间戳；end=True 时补到当天 23:59:59。"""
    text = (text or "").strip()
    if not text:
        return 0
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    if end and len(text) <= 10:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp())


def cmd_resolve(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config)) if args.config else {}
    engine = build_engine(cfg) if cfg else _bare_engine()
    if args.from_url:
        from .resolve import biz_from_url

        got = biz_from_url(engine, args.from_url)
    else:
        from .resolve import resolve_account

        got = resolve_account(engine, args.name, max_hits=args.max_hits, logger=print)
    if not got:
        print("没解析出来。换一条更具体的关键词，或直接给一篇文章链接用 --from-url。")
        return 1
    print(json.dumps(got.to_dict(), ensure_ascii=False, indent=1))
    return 0


def cmd_admin_login(args: argparse.Namespace) -> int:
    """打开后台让管理员扫码，落盘 cookie + token。"""
    from .mp_admin import login_via_browser

    sess = login_via_browser(
        profile_key=args.profile,
        headless=args.headless,
        timeout_s=float(args.timeout),
        save_path=Path(args.save),
        channel=args.browser_channel,
        logger=print,
    )
    if not sess:
        print("没有拿到登录态。确认已扫码并进入后台首页（URL 里应有 token=...）")
        return 1
    print(f"已保存后台登录态：{args.save}（仅当前用户可读写）")
    return 0


def cmd_admin(args: argparse.Namespace) -> int:
    """后台发表记录：全量清单 + 阅读/点赞 + 可选正文。"""
    from .mp_admin import AdminClient, AdminError, AdminSession

    path = Path(args.session)
    if not path.exists():
        print(f"缺少登录态文件：{path}（先跑 admin-login 或手写 cookie/token）")
        return 2
    sess = AdminSession.load(path)
    if not sess.ok:
        print("登录态不完整：需要 cookie 和 token")
        return 2

    cfg = load_config(Path(args.config)) if args.config else {}
    engine = build_engine(cfg) if cfg else _bare_engine()
    since = _to_ts(args.since, end=False)
    until = _to_ts(args.until, end=True)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    index = out / "admin-index.jsonl"

    client = AdminClient(engine, sess)
    if args.dump_raw:
        text, ret, total, items = client.fetch_page(0)
        Path(args.dump_raw).write_text(text, "utf-8")
        print(f"原始响应已存 {args.dump_raw}（ret={ret}, total={total}, items={len(items)}）")

    from .textmode import TextRun

    text_run = TextRun(engine, out, pause=(0.5, 1.2)) if args.text else None
    n = 0
    body_failed = 0
    try:
        with index.open("a", encoding="utf-8") as fh:
            for item in client.iter_published(max_pages=args.max_pages, since=since, until=until):
                n += 1
                rec = item.to_dict()
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(
                    f"{rec['publish_date']}  阅读 {item.read_num:>6}  赞 {item.like_num:>4}  "
                    f"{item.title[:36]}"
                )
                if text_run is not None and not item.url:
                    body_failed += 1
                    continue
                if text_run is not None and item.url:
                    cached = text_run.cached_result(item.url)
                    if cached:
                        continue
                    art, error = text_run.fetch(item.url)
                    if art is None:
                        body_failed += 1
                        print(redact(f"正文失败：{item.title}：{error}"))
                        continue
                    art.title = art.title or item.title
                    art.publish_ts = art.publish_ts or item.publish_ts
                    text_run.save(art, item.url)
    except AdminError as exc:
        print(f"后台报错：{exc}")
        return 3

    finally:
        if client.report.stop_reason == "running":
            client.report.stop("interrupted")
        atomic_write(out / "collection-report.json", json.dumps({
            "mode": "admin", "coverage": client.report.as_dict(), "items": n,
            "body_failed": body_failed, "body_requested": args.text,
            "archive_complete": bool(args.text and client.report.coverage_complete and not body_failed),
        }, ensure_ascii=False, indent=2))
    if args.comments:
        rows = client.latest_comments(count=20)
        cpath = out / "admin-comments.jsonl"
        with cpath.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"留言：{sum(len(r['comments']) for r in rows)} 条 / {len(rows)} 篇 -> {cpath}")

    print(json.dumps({"items": n, "body_failed": body_failed, "index": str(index)}, ensure_ascii=False))
    return 0 if n and not body_failed else 1


def cmd_search(args: argparse.Namespace) -> int:
    """按名字枚举搜索结果（零凭证），可选按日期过滤并落文本。"""
    cfg = load_config(Path(args.config)) if args.config else {}
    engine = build_engine(cfg) if cfg else _bare_engine()
    since = _to_ts(args.since, end=False)
    until = _to_ts(args.until, end=True)

    from .search import search_articles
    from .textmode import TextRun

    out = Path(args.out)
    run = TextRun(engine, out, pause=(0.5, 1.2)) if args.text else None
    hits: list = []

    def show(hit) -> None:
        hits.append(hit)
        day = datetime.fromtimestamp(hit.publish_ts, CST).strftime("%Y-%m-%d") if hit.publish_ts else "?"
        print(f"HIT  {day}  {hit.account} · {hit.title[:40]}  {hit.url[:70]}")

    result = search_articles(
        engine,
        args.name,
        since=since,
        until=until,
        max_pages=args.max_pages,
        per_page=args.per_page,
        exact=not args.fuzzy,
        pause=(float(args.pause[0]), float(args.pause[1])),
        logger=print,
        on_hit=show,
    )

    out.mkdir(parents=True, exist_ok=True)
    index = out / "search-index.jsonl"
    with index.open("a", encoding="utf-8") as fh:
        for hit in result.hits:
            fh.write(json.dumps(hit.to_dict(), ensure_ascii=False) + "\n")
            if run is not None and hit.article is not None:
                run.save(hit.article, hit.url)

    atomic_write(out / "collection-report.json", json.dumps({"mode": "search", **result.summary()}, ensure_ascii=False, indent=2))
    print(json.dumps({"summary": result.summary(), "index": str(index)}, ensure_ascii=False, indent=1))
    return 0 if result.hits else 1


def cmd_album(args: argparse.Namespace) -> int:
    from .album import AlbumJob, AlbumTarget
    target = AlbumTarget.from_url(args.url) if args.url else AlbumTarget(args.biz or "", args.album_id or "")
    cfg = load_config(Path(args.config)) if args.config else {}
    engine = build_engine(cfg) if cfg else _bare_engine()
    job = AlbumJob(engine, args.out, target, save_raw=args.raw, expected_title=args.expect_title)
    result = job.run(max_pages=args.max_pages, max_articles=args.max_articles,
                     list_only=args.list_only, restart=args.restart)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    success = result["coverage"]["coverage_complete"] if args.list_only else result["archive_complete"]
    return 0 if success else 1


def cmd_text(args: argparse.Namespace) -> int:
    """只抓正文：吃 URL 清单，不需要公众号凭证。"""
    cfg = load_config(Path(args.config)) if args.config else {}
    engine = build_engine(cfg) if cfg else _bare_engine()
    urls: list[str] = list(args.url or [])
    if args.urls:
        urls += [
            line.strip()
            for line in Path(args.urls).read_text("utf-8", errors="replace").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    from .textmode import TextRun

    run = TextRun(
        engine,
        Path(args.out),
        pause=(float(args.pause[0]), float(args.pause[1])),
        save_raw=args.raw,
        force=args.force,
    )
    if args.retry_failed:
        urls += run.retry_urls()
    if not urls:
        if args.retry_failed:
            print("最近一次任务没有失败或未处理链接")
            return 0
        print("没有待处理 URL：用 --url、--urls 或 --retry-failed")
        return 2

    def show(res) -> None:
        if res.skipped:
            print(f"SKIP  已归档：{res.path}")
        elif res.ok:
            print(f"OK   {res.chars:>5}字  {res.account} · {res.title[:38]}  -> {res.path}")
        else:
            print(f"FAIL             {res.url[:70]}  {res.error}")

    results = run.run(urls, on_item=show)
    ok = sum(1 for r in results if r.ok)
    print(f"\n完成：{ok}/{len(results)} 篇，输出目录 {Path(args.out).resolve()}")
    return 0 if results and ok == len(results) else 1


def _bare_engine() -> RiskEngine:
    """没有 config 时的最小引擎：直连、低频、2 次重试。"""
    return _track_engine(RiskEngine(
        proxies=[""],
        accounts=[],
        pacer=AdaptivePacer(base_interval=2.0, jitter_ratio=0.5),
        breaker=CircuitBreaker(threshold=6, open_seconds=300),
        bucket_rate=0.5,
        max_attempts=3,
    ))


def cmd_parse(args: argparse.Namespace) -> int:
    raw = Path(args.file).read_text("utf-8", errors="replace")
    art = parse_article(raw, args.url or "")
    print(json.dumps(art.to_dict(), ensure_ascii=False, indent=1))
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    engine = build_engine(cfg)
    print(json.dumps(engine.snapshot(), ensure_ascii=False, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser("lingyi-shiwen", description="零一拾文 · 公众号自适应采集引擎")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("crawl", help="按 __biz 拉历史列表并归档正文")
    c.add_argument("--config", default="config.json")
    c.add_argument("--biz", default="")
    c.add_argument("--name", default="", help="公众号名字（搜狗解析 biz，可能失败）")
    c.add_argument("--from-url", dest="from_url", default="", help="给一篇文章链接，从页面解析 biz（最稳）")
    c.add_argument("--since", default="", help="起始日期 2025-01-01")
    c.add_argument("--until", default="", help="结束日期 2025-06-30（含当天）")
    c.add_argument("--out", default="./archive")
    c.add_argument("--limit", type=int, default=100)
    c.add_argument("--max-pages", type=int, default=50)
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--update", action="store_true", help="从最新页检查新增文章，保留历史任务断点")
    c.add_argument("--no-resume", action="store_true")
    c.set_defaults(func=cmd_crawl)

    d = sub.add_parser("parse", help="离线解析一篇文章 HTML")
    d.add_argument("--file", required=True)
    d.add_argument("--url", default="")
    d.set_defaults(func=cmd_parse)

    al = sub.add_parser("admin-login", help="扫码登录公众号后台，保存 cookie+token")
    al.add_argument("--save", default="./admin.json")
    al.add_argument("--profile", default="mp-admin", help="浏览器 profile（复用登录态）")
    al.add_argument("--headless", action="store_true", help="默认有头，方便扫码")
    al.add_argument("--timeout", type=float, default=240)
    al.add_argument("--browser-channel", default="chromium", choices=["chromium", "chrome", "msedge"])
    al.set_defaults(func=cmd_admin_login)

    ad = sub.add_parser("admin", help="后台发表记录：全量清单 + 阅读/点赞（需登录态）")
    ad.add_argument("--session", default="./admin.json")
    ad.add_argument("--since", default="")
    ad.add_argument("--until", default="")
    ad.add_argument("--max-pages", dest="max_pages", type=int, default=20)
    ad.add_argument("--out", default="./admin-out")
    ad.add_argument("--text", action="store_true", help="同时抓正文（零凭证）")
    ad.add_argument("--dump-raw", dest="dump_raw", default="", help="保存第一页原始响应，用于字段校准")
    ad.add_argument("--comments", action="store_true", help="同时拉后台最新留言（含未精选）")
    ad.add_argument("--config", default="")
    ad.set_defaults(func=cmd_admin)

    se = sub.add_parser("search", help="按名字枚举搜索结果（零凭证，可筛日期）")
    se.add_argument("--name", required=True)
    se.add_argument("--since", default="")
    se.add_argument("--until", default="")
    se.add_argument("--max-pages", dest="max_pages", type=int, default=6)
    se.add_argument("--per-page", dest="per_page", type=int, default=10)
    se.add_argument("--out", default="./search-out")
    se.add_argument("--text", action="store_true", help="同时保存 Markdown")
    se.add_argument("--fuzzy", action="store_true", help="名字用包含匹配（默认精确）")
    se.add_argument("--pause", nargs=2, type=float, default=(1.5, 3.0))
    se.add_argument("--config", default="")
    se.set_defaults(func=cmd_search)

    r = sub.add_parser("resolve", help="公众号名 / 文章链接 → __biz")
    r.add_argument("--name", default="")
    r.add_argument("--from-url", dest="from_url", default="")
    r.add_argument("--max-hits", dest="max_hits", type=int, default=5)
    r.add_argument("--config", default="")
    r.set_defaults(func=cmd_resolve)

    album = sub.add_parser("album", help="指定合集，自动翻页并归档全部可访问文章")
    identity = album.add_mutually_exclusive_group(required=True)
    identity.add_argument("--url", help="合集分享链接")
    identity.add_argument("--album-id", help="数字合集 ID，需同时提供 --biz")
    album.add_argument("--biz", default="")
    album.add_argument("--expect-title", default="", help="可选：校验合集标题，防止抓错")
    album.add_argument("--config", default="")
    album.add_argument("--out", required=True, help="独立的合集归档目录")
    album.add_argument("--raw", action="store_true")
    album.add_argument("--list-only", action="store_true", help="只枚举清单，保留后续正文任务")
    album.add_argument("--restart", action="store_true", help="重新枚举合集，复用正文成功索引")
    album.add_argument("--max-pages", type=int, default=10000)
    album.add_argument("--max-articles", type=int, default=0, help="本次正文处理上限；0 表示不限制")
    album.set_defaults(func=cmd_album)

    t = sub.add_parser("text", help="只抓正文文本（URL 清单，不需凭证）")
    t.add_argument("--url", action="append", default=[], help="可重复")
    t.add_argument("--urls", default="", help="URL 文件，一行一个，# 开头跳过")
    t.add_argument("--out", default="./text-out")
    t.add_argument("--config", default="", help="可选：复用其中的代理/节流/浏览器配置")
    t.add_argument("--force", action="store_true", help="重新抓取已有正文")
    t.add_argument("--retry-failed", action="store_true", help="从同一输出目录报告中恢复失败与未处理链接")
    t.add_argument("--raw", action="store_true", help="同时保存正文 HTML")
    t.add_argument("--pause", nargs=2, type=float, default=(1.5, 3.5), help="篇间停顿区间（秒）")
    t.set_defaults(func=cmd_text)

    s = sub.add_parser("snapshot", help="打印池子/节流状态")
    s.add_argument("--config", default="config.json")
    s.set_defaults(func=cmd_snapshot)

    args = p.parse_args(argv)
    try:
        with output_lock(args.out) if getattr(args, "out", None) else nullcontext():
            return int(args.func(args) or 0)
    except (ValueError, OSError) as exc:
        print(redact(f"输入或环境错误：{exc}"), file=sys.stderr)
        return 2
    except (RiskEngineError, AdminError) as exc:
        print(redact(f"任务暂停：{exc}"), file=sys.stderr)
        return 3
    finally:
        while _ACTIVE_ENGINES:
            _ACTIVE_ENGINES.pop().close()


if __name__ == "__main__":
    sys.exit(main())
