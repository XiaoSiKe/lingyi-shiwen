"""指纹浏览器桥：直接复用 `零一智译/browser-core` 的内核，不重写注入。

分工：
- 零一智译 browser-core（Node 22，零依赖）负责真实 Chrome 拉起、CDP Emulation、
  document_start 指纹注入、per-profile 独占 user-data-dir、persona 跨维一致性；
- 这里只做三件事：起子进程、按行 JSON 收发、把结果塞进 `RiskEngine.browser_hook`。

为什么不把 `fingerprint/inject.mjs`（1750 行）翻成 Python：
那道注入里 31 条跨维一致性规则、`Function.toString` 保真、WebRTC IP 伪装都是踩过坑的，
复刻一遍只会引入新的自相矛盾；桥接还能跟着它升级。

协议见 `bridge/wx_kernel_bridge.mjs` 顶部注释。
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

BRIDGE = Path(__file__).resolve().parents[1] / "bridge" / "wx_kernel_bridge.mjs"


class KernelBridgeError(RuntimeError):
    pass


def default_core_path() -> Path | None:
    """按 env → 同级目录 → 桌面 的顺序找 browser-core。"""
    env = os.environ.get("LINGYI_BROWSER_CORE")
    cands: list[Path] = []
    if env:
        cands.append(Path(env))
    here = Path(__file__).resolve()
    for base in (here.parents[2], here.parents[3] if len(here.parents) > 3 else here.parents[2]):
        cands.append(base / "零一智译" / "browser-core")
    cands.append(Path.cwd() / "browser-core")
    for cand in cands:
        if (cand / "src" / "kernel.mjs").exists():
            return cand
    return None


def default_data_root() -> Path:
    env = os.environ.get("WXRC_BROWSER_DATA_ROOT")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "wxrc" / "browser-profiles"


def node_path() -> str | None:
    return shutil.which("node")


def available(core_path: str | Path | None = None) -> bool:
    core = Path(core_path) if core_path else default_core_path()
    return bool(core and (core / "src" / "kernel.mjs").exists() and BRIDGE.exists() and node_path())


def profile_id_for(key: str) -> str:
    """稳定映射：同一个 key（公众号 biz / 账号名）永远同一个指纹画像。"""
    digest = hashlib.md5((key or "wxrc").encode("utf-8")).hexdigest()[:8]
    return f"p_{digest}"


# --------------------------------------------------------------------------- #
class KernelBridge:
    """一条 stdio 管道，一个浏览器实例；调用是串行的。"""

    def __init__(
        self,
        *,
        core_path: str | Path | None = None,
        data_root: str | Path | None = None,
        headless: bool = True,
        chrome: str = "",
        node: str | None = None,
        logger: Callable[[str], None] | None = None,
        start_timeout: float = 90.0,
    ):
        self.core_path = Path(core_path) if core_path else default_core_path()
        self.data_root = Path(data_root) if data_root else default_data_root()
        self.headless = headless
        self.chrome = chrome
        self.node = node or node_path() or "node"
        self.log = logger or (lambda _m: None)
        self.start_timeout = start_timeout

        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._next_id = 1
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def start(self) -> "KernelBridge":
        if self._proc and self._proc.poll() is None:
            return self
        if not available(self.core_path):
            raise KernelBridgeError(
                f"browser-core 不可用：core={self.core_path} node={node_path()}"
            )
        self.data_root.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.node,
            str(BRIDGE),
            "--core",
            str(self.core_path),
            "--data-root",
            str(self.data_root),
            "--headless" if self.headless else "--headed",
        ]
        if self.chrome:
            cmd += ["--chrome", self.chrome]

        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=flags,
        )
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        self.log(f"[kernel] 启动桥：node={self.node} core={self.core_path}")
        return self

    def _pump_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _pump_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            text = line.strip()
            if text and ("Error" in text or "error" in text or "失败" in text):
                self.log(f"[kernel:stderr] {text[:300]}")

    # ------------------------------------------------------------------ #
    def _call(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self._lock:
            self.start()
            assert self._proc and self._proc.stdin
            rid = self._next_id
            self._next_id += 1
            payload = {**payload, "id": rid}
            try:
                self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, ValueError) as exc:
                raise KernelBridgeError(f"桥进程已退出：{exc}") from exc

            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise KernelBridgeError(f"{payload.get('cmd')} 超时（{timeout:.0f}s）")
                try:
                    line = self._lines.get(timeout=left)
                except queue.Empty:
                    raise KernelBridgeError(f"{payload.get('cmd')} 超时（{timeout:.0f}s）") from None
                if line is None:
                    raise KernelBridgeError("桥进程意外结束")
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") != rid:
                    continue  # 迟到的响应（例如上一条超时）直接丢
                if not msg.get("ok"):
                    if msg.get("fatal"):
                        raise KernelBridgeError(str(msg.get("error")))
                    raise KernelBridgeError(str(msg.get("error") or "未知错误"))
                return msg.get("data") or {}

    # ------------------------------------------------------------------ #
    def ping(self) -> dict:
        return self._call({"cmd": "ping"}, timeout=30)

    def open(
        self,
        url: str,
        *,
        referer: str = "https://mp.weixin.qq.com/",
        profile_key: str = "wxrc",
        seed: int | None = None,
        locale: str = "zh-CN",
        proxy: dict | None = None,
        wait_human_ms: int = 0,
        timeout_ms: int = 20000,
        total_timeout: float | None = None,
    ) -> dict:
        payload = {
            "cmd": "open",
            "profileId": profile_id_for(profile_key),
            "name": profile_key[:40],
            "seed": seed if seed is not None else int(hashlib.md5(profile_key.encode()).hexdigest()[:8], 16),
            "locale": locale,
            "url": url,
            "referer": referer,
            "proxy": proxy,
            "waitHumanMs": int(wait_human_ms),
            "timeoutMs": int(timeout_ms),
        }
        budget = total_timeout or max(self.start_timeout + 30, timeout_ms / 1000 + wait_human_ms / 1000 + 60)
        return self._call(payload, timeout=budget)

    def evaluate(self, expr: str) -> Any:
        return self._call({"cmd": "eval", "expr": expr}, timeout=60).get("value")

    def cookies(self, urls: list[str]) -> list[dict]:
        return self._call({"cmd": "cookies", "urls": urls}, timeout=30).get("cookies") or []

    def close_page(self) -> None:
        try:
            self._call({"cmd": "close"}, timeout=30)
        except KernelBridgeError:
            pass

    def shutdown(self) -> None:
        proc = self._proc
        if not proc:
            return
        try:
            self._call({"cmd": "shutdown"}, timeout=15)
        except KernelBridgeError:
            pass
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                proc.kill()
        self._proc = None

    def __enter__(self) -> "KernelBridge":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.shutdown()


# --------------------------------------------------------------------------- #
def make_kernel_hook(
    *,
    core_path: str | Path | None = None,
    data_root: str | Path | None = None,
    headless: bool = True,
    chrome: str = "",
    profile_key: str = "",
    locale: str = "zh-CN",
    proxy: dict | None = None,
    wait_human_ms: int = 90_000,
    timeout_ms: int = 25_000,
    logger: Callable[[str], None] | None = None,
) -> Callable[[str, str], tuple[int, str, str] | None]:
    """产出 `RiskEngine(browser_hook=...)` 需要的函数。

    profile_key 决定用哪个指纹画像：同一公众号/同一账号传同一个值，
    换号就是换画像（互不相同的 canvas/webgl/audio 噪声种子）。
    """
    bridge = KernelBridge(
        core_path=core_path, data_root=data_root, headless=headless, chrome=chrome, logger=logger
    )
    log = logger or (lambda _m: None)

    def hook(url: str, referer: str, *, proxy_url: str | None = None):
        current_proxy = proxy
        if proxy_url is not None:
            from urllib.parse import urlsplit
            current_proxy = None
            if proxy_url:
                parsed = urlsplit(proxy_url)
                if parsed.username or parsed.password:
                    raise KernelBridgeError("内核桥不支持带凭证代理，保留原出口并暂停")
                current_proxy = {"mode": parsed.scheme.replace("socks5h", "socks5"), "host": parsed.hostname, "port": parsed.port}
        try:
            data = bridge.open(
                url,
                referer=referer,
                profile_key=profile_key or _key_from_url(url),
                locale=locale,
                proxy=current_proxy,
                wait_human_ms=wait_human_ms,
                timeout_ms=timeout_ms,
            )
        except KernelBridgeError as exc:
            log(f"[kernel] 打开失败：{exc}")
            return None
        html = data.get("html") or ""
        final_url = data.get("finalUrl") or url
        status = int(data.get("status") or 200)
        if data.get("cookies"):
            hook.cookies = data["cookies"]  # type: ignore[attr-defined]
        return status, html, final_url

    hook.bridge = bridge  # type: ignore[attr-defined]
    hook.close = bridge.shutdown  # type: ignore[attr-defined]
    hook.supports_proxy = True
    return hook


def _key_from_url(url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(url).query)
    biz = (q.get("__biz") or [""])[0]
    if biz:
        return biz
    path = urlparse(url).path
    return path.rsplit("/", 1)[-1] or "wxrc"
