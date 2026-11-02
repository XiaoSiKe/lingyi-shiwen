#!/usr/bin/env python3
"""零一拾文的 Skill 源码入口；不依赖项目打包或当前工作目录。"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def doctor(*, require_browser: bool = False) -> int:
    dependencies = {name: importlib.util.find_spec(name) is not None for name in ("httpx", "lxml")}
    ready = sys.version_info >= (3, 10) and all(dependencies.values())
    report = {
        "name": "零一拾文 · 公众号自适应采集引擎",
        "prototype_developer": "Spark",
        "python": sys.version.split()[0],
        "source_root": str(ROOT),
        "dependencies": dependencies,
        "ready": ready,
    }
    if all(dependencies.values()):
        from wxrc.browser import playwright_available
        report["optional_playwright"] = playwright_available()
    if require_browser:
        report["browser_ready"] = False
        if report.get("optional_playwright"):
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    report["chromium_executable"] = pw.chromium.executable_path
                    report["browser_ready"] = Path(pw.chromium.executable_path).is_file()
            except Exception as exc:
                report["browser_error"] = str(exc)
        if not report["browser_ready"]:
            report["browser_next_step"] = "在当前 Python 环境安装 requirements-browser.txt，再运行 python -m playwright install chromium"
        ready = ready and report["browser_ready"]
    if not report["ready"]:
        report["next_step"] = "使用 Python 3.10+；在此源码目录的独立虚拟环境中安装 requirements.txt，然后用该环境的 Python 运行本脚本。"
    report["core_ready"] = report["ready"]
    report["ready"] = ready
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ready else 2


def main() -> int:
    if sys.argv[1:] in (["doctor"], ["doctor", "--browser"]):
        return doctor(require_browser="--browser" in sys.argv)
    if sys.argv[1:] == ["--version"]:
        # 版本从源码声明读取，避免引入需要运行依赖的包初始化。
        import ast
        tree = ast.parse((ROOT / "wxrc" / "__init__.py").read_text("utf-8"))
        version = next(ast.literal_eval(n.value) for n in tree.body
                       if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in n.targets))
        print(f"零一拾文 · 公众号自适应采集引擎 {version}")
        return 0
    if not all(importlib.util.find_spec(name) is not None for name in ("httpx", "lxml")):
        return doctor()
    from wxrc.cli import main as run_core
    return run_core()


if __name__ == "__main__":
    raise SystemExit(main())
