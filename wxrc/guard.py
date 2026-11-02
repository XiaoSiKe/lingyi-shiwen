"""风控判定层：把「页面/JSON/状态码」翻译成可执行的处置动作。

微信的拦截信号分散在三处，必须一起看：
1) HTTP 层：302 -> /mp/verify、wappoc_appmsgcaptcha、429、503；
2) HTML 层：「环境异常」「完成验证后即可继续访问」「请在微信客户端打开链接」；
3) 接口层：ret=-3（频繁）/ret=-6（风控）/200003（key 失效）/200013（频率）/200002（参数）。

判定结果不是布尔，而是「风险等级 + 该换哪一层身份 + 冷却多久」，
这样上层可以按阶梯轮换，而不是一遇风控就整段停摆。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re

from lxml import html as lxml_html


class Risk(str, Enum):
    OK = "ok"              # 正常
    SOFT = "soft"          # 网络/5xx，原身份重试即可
    RATE = "rate"          # 频率/行为风控：换指纹 + 冷却当前号
    CRED = "cred"          # 凭证失效：必须换号 / 重新取 key
    CAPTCHA = "captcha"    # 验证码/环境异常：换出口 IP + 换指纹 + 降频
    GONE = "gone"          # 文章已删/违规，不可重试
    LOGIN = "login"        # 需登录态（wap_sid2 过期）


# 命中即判 captcha 的 URL 特征（302 目标）
CAPTCHA_URL_MARKERS = (
    "wappoc_appmsgcaptcha",
    "/mp/verify",
    "antispider",
    "weixin110",
    "secitptpage",
)

CAPTCHA_TEXT_MARKERS = (
    "环境异常",
    "完成验证后即可继续访问",
    "请输入验证码",
    "请依次点击",
    "当前环境异常",
    "去验证",
)

LOGIN_TEXT_MARKERS = (
    "请在微信客户端打开链接",
    "请在微信中打开",
    "登录超时",
    "请重新登录",
    "invalid session",
)

GONE_TEXT_MARKERS = (
    "该内容已被发布者删除",
    "此内容因违规无法查看",
    "该公众号已迁移",
    "内容已删除",
    "该文章已被删除",
    "此帐号已被屏蔽",
)

# 接口 ret 码映射
RET_RATE = {-3, -6, 200013, 200012}
RET_CRED = {200003, 200002}
RET_GONE = {200005}


def _cool(kind: Risk, base: dict[str, float]) -> float:
    return float(base.get(kind.value, 0.0))


@dataclass
class Verdict:
    risk: Risk = Risk.OK
    reason: str = ""
    retry_after: float = 0.0     # 该身份应冷却的秒数（0=可立即复用）
    rotate_ip: bool = False
    rotate_account: bool = False
    rotate_fingerprint: bool = False
    dead_account: bool = False   # 需要重新取凭证
    evidence: str = ""

    @property
    def ok(self) -> bool:
        return self.risk is Risk.OK

    def __bool__(self) -> bool:  # 便于 if verdict:
        return self.ok


def classify_json(payload: dict | None, *, cool: dict[str, float] | None = None) -> Verdict:
    """getmsg / appmsg 类接口的 ret 码判定。"""
    cool = cool or {}
    if not isinstance(payload, dict):
        return Verdict(Risk.SOFT, "响应不是 JSON", retry_after=5.0)

    base = payload.get("base_resp") or {}
    if not isinstance(base, dict):
        base = {}
    ret = payload.get("ret", base.get("ret", 0))
    try:
        ret = int(ret)
    except (TypeError, ValueError):
        ret = 0
    errmsg = str(payload.get("errmsg") or base.get("errmsg") or "")

    if ret == 0:
        return Verdict(Risk.OK, "", evidence=errmsg[:120])

    low = errmsg.lower()
    # 评论/阅读数接口在无会话时返回 ret=-3 + "no session"：这是凭证问题，不是频率问题
    if "no session" in low or ("session" in low and "expired" in low):
        return Verdict(
            Risk.CRED,
            f"ret={ret} 无会话（需 key/pass_ticket + cookie）",
            retry_after=_cool(Risk.CRED, cool),
            rotate_account=True,
            dead_account=True,
            evidence=errmsg[:120],
        )

    if ret in RET_CRED or "invalid csrf" in errmsg.lower() or "key" in errmsg.lower():
        return Verdict(
            Risk.CRED,
            f"ret={ret} 凭证失效",
            retry_after=_cool(Risk.CRED, cool),
            rotate_account=True,
            dead_account=True,
            evidence=errmsg[:120],
        )
    if ret in RET_GONE:
        return Verdict(Risk.GONE, f"ret={ret} 内容不可用", rotate_account=True, evidence=errmsg[:120])
    if ret in RET_RATE or ret < 0:
        return Verdict(
            Risk.RATE,
            f"ret={ret} 频率/风控",
            retry_after=_cool(Risk.RATE, cool),
            rotate_fingerprint=True,
            rotate_account=ret == -6,
            evidence=errmsg[:120],
        )
    return Verdict(Risk.RATE, f"未知 ret={ret}", retry_after=_cool(Risk.RATE, cool), evidence=errmsg[:120])


def classify_html(
    html: str = "",
    *,
    url: str = "",
    final_url: str = "",
    status: int = 200,
    location: str = "",
    cool: dict[str, float] | None = None,
) -> Verdict:
    """文章页 / 验证页判定。location 用于「不跟随重定向」的传输层。"""
    cool = cool or {}
    haystack_url = f"{url} {final_url} {location}".lower()

    if any(m in haystack_url for m in CAPTCHA_URL_MARKERS):
        return Verdict(
            Risk.CAPTCHA,
            "重定向到验证页",
            retry_after=_cool(Risk.CAPTCHA, cool),
            rotate_ip=True,
            rotate_fingerprint=True,
            evidence=(final_url or location)[:160],
        )

    if 300 <= status < 400:
        # 302 且不是验证页：说明传输层没跟随（curl_cffi 等），交给上层重试跟随
        return Verdict(Risk.SOFT, f"HTTP {status} 未跟随重定向", retry_after=2.0,
                       evidence=location[:160])

    if status == 404:
        return Verdict(Risk.GONE, "HTTP 404 页面不存在", evidence="404")
    if status == 429:
        return Verdict(
            Risk.RATE, "HTTP 429", retry_after=_cool(Risk.RATE, cool),
            rotate_fingerprint=True, evidence="429",
        )
    if status == 403:
        return Verdict(
            Risk.CAPTCHA, f"HTTP {status}", retry_after=_cool(Risk.CAPTCHA, cool),
            rotate_ip=True, rotate_fingerprint=True, evidence=str(status),
        )

    if status >= 500 or status <= 0:
        return Verdict(Risk.SOFT, f"HTTP {status} 服务或连接异常", retry_after=5.0)
    if status >= 400:
        return Verdict(Risk.SOFT, f"HTTP {status}", retry_after=5.0)

    # 文章正文中可以正常讨论验证与登录；只在非文章页检查提示文案。
    try:
        doc = lxml_html.fromstring(html or "<html></html>")
        if doc.xpath('//*[@id="js_content"]'):
            return Verdict(Risk.OK)
        for node in doc.xpath('//script|//style'):
            node.drop_tree()
        head = " ".join(doc.itertext())[:160_000]
    except (ValueError, lxml_html.etree.ParserError):
        head = re.sub(r"<[^>]*>", " ", (html or "")[:160_000])

    for marker in CAPTCHA_TEXT_MARKERS:
        if marker in head:
            return Verdict(
                Risk.CAPTCHA, f"命中验证文案：{marker}",
                retry_after=_cool(Risk.CAPTCHA, cool),
                rotate_ip=True, rotate_fingerprint=True, evidence=marker,
            )
    for marker in LOGIN_TEXT_MARKERS:
        if marker in head:
            return Verdict(
                Risk.LOGIN, f"命中登录文案：{marker}",
                retry_after=_cool(Risk.LOGIN, cool),
                rotate_account=True, evidence=marker,
            )
    for marker in GONE_TEXT_MARKERS:
        if marker in head:
            return Verdict(Risk.GONE, f"内容不可用：{marker}", evidence=marker)

    return Verdict(Risk.OK)


def needs_browser(verdict: Verdict, captcha_hits: int, threshold: int = 2) -> bool:
    """同一 IP 连续被验证码拦 N 次 → 升级到真实浏览器（人工过验证 / 拿 cookie）。"""
    return verdict.risk is Risk.CAPTCHA and captcha_hits >= threshold
