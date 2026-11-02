"""第二轮独立修复：速率预约、完整输出与浏览器诊断。"""
import json
from types import SimpleNamespace

import pytest

from wxrc import cli
from wxrc.limiter import TokenBucket
from wxrc.metrics import MetricsClient
from wxrc.account_pool import Account
from wxrc.session import Reply
from wxrc.guard import Risk, Verdict


def test_bucket_reservations_are_not_double_credited(monkeypatch):
    now = [100.0]
    monkeypatch.setattr('wxrc.limiter.time.monotonic', lambda: now[0])
    bucket = TokenBucket(rate=1, burst=1)
    assert bucket.take() == 0
    assert bucket.take() == 1
    now[0] += 1
    assert bucket.take() == 1
    assert bucket.take() == 2


def test_parse_emits_complete_json(tmp_path, capsys):
    source = tmp_path / 'article.html'
    body = '正文' * 5000
    source.write_text(f'<h1 id="activity-name">长文章</h1><div id="js_content">{body}</div>')
    assert cli.main(['parse', '--file', str(source)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['content_text'] == body


def test_partial_batch_has_failure_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, '_bare_engine', lambda: None)
    monkeypatch.setattr('wxrc.textmode.TextRun.run', lambda *a, **kw: [SimpleNamespace(ok=True), SimpleNamespace(ok=False)])
    assert cli.main(['text','--url','https://example.invalid','--out',str(tmp_path)]) == 1


def test_http_error_metrics_not_accepted_as_success():
    reply = Reply(status=500, text='', url='', final_url='', payload={'appmsgstat':{'read_num':1}},
                  verdict=Verdict(Risk.SOFT, 'HTTP 500'))
    engine = SimpleNamespace(get=lambda *a, **kw: reply)
    got = MetricsClient(engine).read_stats(biz='B',mid='1',idx='1',sn='s',account=Account(name='a'))
    assert not got.ok and 'HTTP 500' in got.reason
