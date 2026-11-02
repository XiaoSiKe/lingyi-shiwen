"""纯文本模式（URL 清单 → Markdown）的验证。"""
from __future__ import annotations

import json
import random

from wxrc.limiter import AdaptivePacer, CircuitBreaker
from wxrc.proxy_pool import ProxyNode, ProxyPool
from wxrc.session import RiskEngine
from wxrc.textmode import TextRun


def make_engine() -> RiskEngine:
    return RiskEngine(
        proxy_pool=ProxyPool(nodes=[ProxyNode(url="", label="direct")]),
        accounts=[],
        pacer=AdaptivePacer(base_interval=0.001, jitter_ratio=0.1),
        breaker=CircuitBreaker(threshold=20, open_seconds=10),
        bucket_rate=1000,
        max_attempts=3,
        sleep=lambda _s: None,
        logger=lambda _m: None,
        rng=random.Random(5),
    )


def test_text_run_writes_markdown(mock, tmp_path):
    state, base = mock
    state.ua_limit = 10          # 本用例只测文本，别让 UA 频控插进来
    urls = [f"{base}/s/art1", f"{base}/s/art2"]

    run = TextRun(make_engine(), tmp_path, pause=(0.001, 0.002), save_raw=True)
    results = run.run(urls)

    assert len(results) == 2 and all(r.ok for r in results)
    assert all(r.chars > 0 for r in results)
    mds = sorted(tmp_path.glob("**/*.md"))
    assert len(mds) == 2
    text = mds[0].read_text("utf-8")
    assert text.startswith("---") and "# 测试文章" in text and "source: " in text
    assert len(list((tmp_path / "raw").rglob("*.html"))) == 2

    recs = [json.loads(x) for x in (tmp_path / "index.jsonl").read_text("utf-8").splitlines()]
    assert len(recs) == 2 and all(r["chars"] > 0 for r in recs)


def test_text_run_reports_failure(mock, tmp_path):
    _state, base = mock
    run = TextRun(make_engine(), tmp_path, pause=(0.001, 0.002))
    results = run.run([f"{base}/missing-page"])

    assert len(results) == 1 and results[0].ok is False
    assert "404" in results[0].error
