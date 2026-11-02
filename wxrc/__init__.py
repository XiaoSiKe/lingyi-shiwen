"""零一拾文 · 公众号自适应采集引擎。

初步雏形开发者：Spark。

分层：
    fingerprint   设备 / UA / 头部指纹池
    proxy_pool    出口池（打分 + 冷却）
    account_pool  凭证号池（配额 + 续期）
    limiter       自适应节流 + 熔断
    guard         风控判定（ret 码 / 验证页 / 文案）
    session       引擎：选身份 -> 节流 -> 发 -> 判定 -> 阶梯轮换
    resolve       公众号名 / 文章链接 -> __biz
    getmsg        历史消息列表（游标 + 断点 + 日期窗口）
    article       正文解析（新旧模板 + 懒加载图片）
    textmode      纯文本模式（URL 清单 -> Markdown，零凭证）
    metrics       互动数据（阅读 / 点赞 / 留言，需会话）
    crawl         编排与落盘
"""
from .account_pool import Account, AccountPool
from .article import Article, parse_article
from .browser import make_browser_hook
from .crawl import ArchiveJob, ArchiveStats
from .fingerprint import Device, make_device
from .getmsg import ArticleRef, Cursor, ProfileCrawler, build_profile_url, parse_getmsg
from .guard import Risk, Verdict, classify_html, classify_json
from .limiter import AdaptivePacer, CircuitBreaker, TokenBucket
from .metrics import Comment, MetricsClient, Stats, parse_appmsgstat, parse_comments
from .mp_admin import AdminClient, AdminError, AdminItem, AdminSession
from .proxy_pool import ProxyNode, ProxyPool
from .resolve import Resolved, biz_from_url, resolve_account
from .search import SearchHit, SearchRun, search_articles
from .session import BreakerOpen, Exhausted, Identity, NoIdentity, Reply, RiskEngine

__all__ = [
    "Account",
    "AccountPool",
    "Article",
    "parse_article",
    "ArchiveJob",
    "ArchiveStats",
    "make_browser_hook",
    "Device",
    "make_device",
    "ArticleRef",
    "Cursor",
    "ProfileCrawler",
    "build_profile_url",
    "parse_getmsg",
    "Risk",
    "Verdict",
    "classify_html",
    "classify_json",
    "AdaptivePacer",
    "CircuitBreaker",
    "TokenBucket",
    "Comment",
    "MetricsClient",
    "Stats",
    "parse_appmsgstat",
    "parse_comments",
    "AdminClient",
    "AdminError",
    "AdminItem",
    "AdminSession",
    "ProxyNode",
    "ProxyPool",
    "Resolved",
    "resolve_account",
    "biz_from_url",
    "SearchHit",
    "SearchRun",
    "search_articles",
    "BreakerOpen",
    "Exhausted",
    "Identity",
    "NoIdentity",
    "Reply",
    "RiskEngine",
]

__version__ = "0.1.9"
