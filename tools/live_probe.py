"""真实站点探针：读一条公开文章，报告「文本 / 阅读 / 留言」各自能不能拿到。

用法：
  python tools/live_probe.py --url https://mp.weixin.qq.com/s/xxxx
  python tools/live_probe.py --url ... --uin U --key K --pass_ticket T   # 有会话再测互动数据
  python tools/live_probe.py --url ... --browser                          # 加测指纹浏览器渲染

只读、低频：一次运行最多 4 个请求（页面 1 + 统计 1 + 评论 1 + 可选浏览器 1）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from wxrc.account_pool import Account, AccountPool
from wxrc.article import parse_article
from wxrc.limiter import AdaptivePacer, CircuitBreaker
from wxrc.metrics import MetricsClient
from wxrc.proxy_pool import ProxyNode, ProxyPool
from wxrc.session import RiskEngine

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
UA_WX = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.50(0x18003228) NetType/WIFI Language/zh_CN"
)
BLOCK_MARKERS = ("环境异常", "完成验证后即可继续访问", "该内容已被发布者删除", "此内容因违规无法查看")


def make_engine(accounts: list[Account] | None = None) -> RiskEngine:
    return RiskEngine(
        proxy_pool=ProxyPool(nodes=[ProxyNode(url="", label="direct")]),
        account_pool=AccountPool(accounts or [], logger=lambda _m: None),
        pacer=AdaptivePacer(base_interval=0.6, jitter_ratio=0.3),
        breaker=CircuitBreaker(threshold=10, open_seconds=120),
        bucket_rate=1.0,
        max_attempts=2,
        logger=lambda _m: None,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--uin", default="")
    ap.add_argument("--key", default="")
    ap.add_argument("--pass-ticket", dest="pass_ticket", default="")
    ap.add_argument("--appmsg-token", dest="appmsg_token", default="")
    ap.add_argument("--browser", action="store_true", help="加测指纹浏览器渲染")
    ap.add_argument("--headless", action="store_true", default=True)
    args = ap.parse_args()

    out: dict = {"url": args.url, "steps": {}}

    # ---- 1) 文本：匿名 httpx ---- #
    engine = make_engine()
    reply = engine.get(args.url, expect="html")
    html = reply.text
    out["steps"]["page"] = {
        "status": reply.status,
        "bytes": len(html),
        "verdict": reply.verdict.risk.value,
        "final_url": reply.final_url,
        "blocked_markers": [m for m in BLOCK_MARKERS if m in html],
    }
    if not reply.verdict.ok:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 1

    art = parse_article(html, args.url)
    out["steps"]["text"] = {
        "title": art.title,
        "account": art.account,
        "publish": art.publish_text or art.publish_ts,
        "chars": len(art.content_text),
        "markdown_chars": len(art.markdown),
        "images": len(art.images),
        "comment_id": art.comment_id,
        "ids": {"biz": art.biz, "mid": art.mid, "idx": art.idx, "sn": art.sn},
    }

    # ---- 2) 互动数据：无会话（故意发一次真实请求，看服务端怎么回） ---- #
    client = MetricsClient(engine)  # 同一个引擎，复用连接
    anon_acc = Account(name="anon", uin="", key="")
    anon_stats = client.read_stats(
        biz=art.biz, mid=art.mid, idx=art.idx, sn=art.sn, account=anon_acc, article_url=args.url,
    )
    anon_items, anon_total, anon_reason = (
        client.comments(
            biz=art.biz, idx=art.idx, comment_id=art.comment_id, account=anon_acc,
            article_url=args.url, limit=20,
        )
        if art.comment_id
        else ([], 0, "页面里没有 comment_id")
    )
    out["steps"]["metrics_anon"] = {
        "stats_reason": anon_stats.reason,
        "read_num": anon_stats.read_num,
        "like_num": anon_stats.like_num,
        "comments": len(anon_items),
        "comments_reason": anon_reason,
    }

    # ---- 3) 互动数据：有会话 ---- #
    if args.key and args.uin:
        acc = Account(
            name="probe", uin=args.uin, key=args.key,
            pass_ticket=args.pass_ticket, appmsg_token=args.appmsg_token,
        )
        engine2 = make_engine([acc])
        client2 = MetricsClient(engine2)
        real = client2.fetch_all(
            biz=art.biz, mid=art.mid, idx=art.idx, sn=art.sn,
            comment_id=art.comment_id, article_url=args.url, comment_limit=20,
        )
        out["steps"]["metrics_session"] = {
            "ok": real["stats"].ok,
            "reason": real["stats"].reason,
            "read_num": real["stats"].read_num,
            "like_num": real["stats"].like_num,
            "old_like_num": real["stats"].old_like_num,
            "comments": len(real["comments"]),
            "total": real["total"],
            "first_comment": real["comments"][0].to_dict() if real["comments"] else None,
        }
    else:
        out["steps"]["metrics_session"] = "跳过：未提供 --uin/--key（互动数据需要会话）"

    if args.browser:
        from wxrc.browser import fetch_via_browser
        result = fetch_via_browser(args.url, headless=args.headless)
        out["steps"]["browser"] = {"status": result[0], "html_bytes": len(result[1])} if result else {"error": "浏览器未能读取页面"}

    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
