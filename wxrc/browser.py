"""零一拾文浏览器支持：标准 Playwright 与持久会话。"""
from __future__ import annotations

from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, unquote

CAPTCHA_MARKERS = ("环境异常", "完成验证后即可继续访问", "wappoc_appmsgcaptcha", "/mp/verify")


def profile_directory(key: str = "default") -> Path:
    import os
    import sys
    from .storage import stable_id
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "lingyi-shiwen" / "browser-profiles" / stable_id(key)


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
    channel: str = "chromium",
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
            channel=None if channel == "chromium" else channel or None,
            headless=headless,
            viewport={"width": 420, "height": 900},
            proxy=proxy,
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                resp = page.goto(url, referer=referer, timeout=timeout_ms, wait_until="domcontentloaded")
            except Exception:
                return None

            waited = 0.0
            step = 1.0
            while waited < wait_human_seconds:
                html = page.content()
                from .guard import classify_html, Risk
                if classify_html(html, final_url=page.url).risk is not Risk.CAPTCHA:
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
    engine: str = "auto", *, logger=None, data_root=None, headless=True,
    chrome: str = "chromium", profile_key: str = "default", locale: str = "zh-CN",
    wait_human_ms: int = 90_000,
):
    engine = (engine or "off").lower()
    if engine in ("off", "none", ""):
        return None
    if engine not in ("auto", "playwright"):
        raise ValueError("browser.engine 仅支持 auto、playwright、off")
    if not playwright_available():
        if engine == "auto":
            return None
        raise ValueError("请安装 requirements-browser.txt 并运行 python -m playwright install chromium")
    return make_playwright_hook(
        headless=headless, channel=chrome or "chromium",
        profile_dir=Path(data_root) if data_root else profile_directory(profile_key),
        wait_human_seconds=wait_human_ms / 1000,
    )
