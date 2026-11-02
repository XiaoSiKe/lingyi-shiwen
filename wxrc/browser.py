"""浏览器兜底层：验证码 / 环境异常的最后一道。

两条实现，默认优先第一条：

1. `kernel` —— 复用 `零一智译/browser-core` 的指纹内核（真实 Chrome + CDP Emulation +
   document_start 注入 + per-profile 独占 user-data-dir）。为什么优先：它保证
   「UA ⟺ UA-CH ⟺ platform ⟺ WebGL/WebGPU ⟺ 时区/语言/Intl ⟺ 屏幕/窗口」这几十个维度
   互相印证，单一 JS 补丁很容易在这层自相矛盾。
2. `playwright` —— 只有 Playwright 时的轻量兜底：持久化 profile + 一个普通 Chrome，
   只解决「人工过一次验证」，不保证指纹自洽。

两者都是 `(url, referer) -> (status, html, final_url) | None` 形状，
直接传给 `RiskEngine(browser_hook=...)`。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, unquote

CAPTCHA_MARKERS = ("环境异常", "完成验证后即可继续访问", "wappoc_appmsgcaptcha", "/mp/verify")


# --------------------------------------------------------------------------- #
# 1) 指纹内核（零一智译 browser-core）
# --------------------------------------------------------------------------- #
def kernel_available(core_path: str | Path | None = None) -> bool:
    from . import kernel_bridge

    return kernel_bridge.available(core_path)


def make_kernel_hook(**kwargs):
    from . import kernel_bridge

    return kernel_bridge.make_kernel_hook(**kwargs)


# --------------------------------------------------------------------------- #
# 2) Playwright 兜底
# --------------------------------------------------------------------------- #
def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except ImportError:
        return False


def fetch_via_browser(
    url: str,
    referer: str = "https://mp.weixin.qq.com/",
    *,
    profile_dir: str | Path = "./.browser-profile",
    channel: str = "chrome",
    headless: bool = False,
    wait_human_seconds: float = 90.0,
    timeout_ms: int = 30_000,
    proxy: dict | None = None,
) -> tuple[int, str, str] | None:
    """返回 (status, html, final_url)；未装 playwright 或失败返回 None。"""
    if not playwright_available():
        return None
    from playwright.sync_api import sync_playwright

    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel=channel or None,
            headless=headless,
            viewport={"width": 420, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            proxy=proxy,
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                resp = page.goto(url, referer=referer, timeout=timeout_ms, wait_until="domcontentloaded")
            except Exception:
                resp = None

            waited = 0.0
            step = 1.0
            while waited < wait_human_seconds:
                html = page.content()
                if not any(m in html for m in CAPTCHA_MARKERS):
                    break
                page.wait_for_timeout(int(step * 1000))
                waited += step
                step = min(3.0, step + 0.5)

            return (resp.status if resp is not None else 200), page.content(), page.url
        finally:
            ctx.close()


def make_playwright_hook(**kwargs) -> Callable[[str, str], tuple[int, str, str] | None]:
    def hook(url: str, referer: str, *, proxy_url: str = ""):
        proxy = None
        if proxy_url:
            parsed = urlsplit(proxy_url)
            proxy = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
            if parsed.username:
                proxy.update(username=unquote(parsed.username), password=unquote(parsed.password or ""))
        return fetch_via_browser(url, referer, **{**kwargs, "proxy": proxy})

    hook.supports_proxy = True
    return hook


# --------------------------------------------------------------------------- #
# 3) 统一入口
# --------------------------------------------------------------------------- #
def make_browser_hook(
    engine: str = "auto",
    *,
    logger: Callable[[str], None] | None = None,
    core_path: str | Path | None = None,
    data_root: str | Path | None = None,
    headless: bool = True,
    chrome: str = "",
    profile_key: str = "",
    locale: str = "zh-CN",
    proxy: dict | None = None,
    wait_human_ms: int = 90_000,
    **playwright_kwargs,
):
    """engine: auto | kernel | playwright | off"""
    engine = (engine or "auto").lower()
    if engine in ("kernel", "auto"):
        if kernel_available(core_path):
            return make_kernel_hook(
                core_path=core_path,
                data_root=data_root,
                headless=headless,
                chrome=chrome,
                profile_key=profile_key,
                locale=locale,
                proxy=proxy,
                wait_human_ms=wait_human_ms,
                logger=logger,
            )
        if engine == "kernel":
            raise RuntimeError(f"browser-core 不可用：{core_path or '(自动探测失败)'}")
    if engine in ("playwright", "auto"):
        if playwright_available():
            return make_playwright_hook(headless=headless, wait_human_seconds=wait_human_ms / 1000, **playwright_kwargs)
        if engine == "playwright":
            raise RuntimeError("未安装 playwright")
    return None
