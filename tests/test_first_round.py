"""第一轮问题回归：从业务入口证明恢复、完整性与暂停规则。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from wxrc.account_pool import Account
from wxrc.article import Article, html_to_markdown, text_of
from wxrc.crawl import ArchiveJob, ArchiveStats, download_images
from wxrc.getmsg import ArticleRef, Cursor, ProfileCrawler
from wxrc.guard import Risk, classify_html, classify_json
from wxrc.limiter import CircuitBreaker
from wxrc.metrics import MetricsClient
from wxrc.mp_admin import AdminClient, AdminItem, AdminSession
from wxrc.proxy_pool import ProxyNode, ProxyPool
from wxrc.security import redact
from wxrc.session import BreakerOpen, NoIdentity, Reply, RiskEngine
from wxrc.textmode import TextRun


class NoWait:
    def wait(self): pass
    def on_success(self): pass
    def on_risk(self, *_args): pass


class Client:
    def __init__(self, body='{"ret":0}', status=200):
        self.body, self.status, self.calls = body, status, []
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return SimpleNamespace(text=self.body, status_code=self.status, url=url, headers={})

    def close(self): self.closed = True


def engine(client=None, **kwargs):
    return RiskEngine(transport_factory=lambda _proxy: client or Client(),
                      pacer=NoWait(), sleep=lambda _s: None, bucket_rate=1e9, **kwargs)


def list_page(count=3):
    return Reply(status=200, text='', url='', final_url='', payload={
        'ret': 0, 'next_offset': 8, 'can_msg_continue': 0,
        'general_msg_list': {'list': [
            {'comm_msg_info': {'datetime': 1700000000}, 'app_msg_ext_info': {
                'title': f't{i}', 'content_url': f'https://mp.weixin.qq.com/s?__biz=B&mid={i}&idx=1'}}
            for i in range(count)]}})


def crawler(tmp_path, monkeypatch):
    monkeypatch.setattr(ProfileCrawler, '_fetch_page', lambda *_args: list_page())
    return ProfileCrawler(engine(), cursor=Cursor(tmp_path / 'cursor.json').load())


def test_cursor_interrupt_replays_unacknowledged_items(tmp_path, monkeypatch):
    iterator = crawler(tmp_path, monkeypatch).crawl('B')
    first = next(iterator)
    iterator.close()  # 下游尚未确认第一篇；模拟进程中断。
    resumed = list(crawler(tmp_path, monkeypatch).crawl('B'))
    assert len(resumed) == 3 and resumed[0].key() == first.key()
    assert not Cursor(tmp_path / 'cursor.json').load().get('B')['pending']


def test_limit_preserves_rest_of_page(tmp_path, monkeypatch):
    one = list(crawler(tmp_path, monkeypatch).crawl('B', limit=1))
    rest = list(crawler(tmp_path, monkeypatch).crawl('B', limit=10))
    assert len(one) == 1 and len(rest) == 2
    assert len({ref.key() for ref in one + rest}) == 3


def test_archive_interruption_keeps_remaining_work(tmp_path, monkeypatch):
    monkeypatch.setattr(ProfileCrawler, '_fetch_page', lambda *_args: list_page())
    job = ArchiveJob(engine(), tmp_path)
    saved = []
    def fetch(ref, stats):
        if saved:
            raise KeyboardInterrupt()
        saved.append(ref.key())
        return Article(title='T', account='A', markdown='正文')
    monkeypatch.setattr(job, 'fetch_article', fetch)
    with pytest.raises(KeyboardInterrupt):
        job.run('B')
    resumed = ArchiveJob(engine(), tmp_path)
    monkeypatch.setattr(resumed, 'fetch_article', lambda ref, stats: Article(title='T', account='A', markdown=ref.key()))
    resumed.run('B')
    rows = [json.loads(line) for line in resumed.index_path.read_text().splitlines()]
    assert len(rows) == 3 and len(list(tmp_path.rglob('*.md'))) == 3


def test_failed_body_retried_even_after_list_done(tmp_path, monkeypatch):
    ref = ArticleRef(title='T', url='https://mp.weixin.qq.com/s/x', biz='B', mid='1', idx='1')
    job = ArchiveJob(engine(), tmp_path)
    job._save(ref, None, ArchiveStats())
    cursor = Cursor(tmp_path / 'cursor.json')
    cursor.data = {'B': {'schema_version': 2, 'scope': [0, 0], 'offset': 8, 'seen': [ref.key()], 'pending': [], 'done': True}}
    cursor.save()
    retry = ArchiveJob(engine(), tmp_path)
    monkeypatch.setattr(retry, 'fetch_article', lambda ref, stats: Article(title=ref.title, account='A', markdown='补采成功'))
    retry.run('B')
    assert ref.key() in ArchiveJob(engine(), tmp_path)._known
    assert '补采成功' in next(tmp_path.rglob('*.md')).read_text()


def test_legacy_cursor_reenumerates_and_respects_success_index(tmp_path, monkeypatch):
    (tmp_path / 'cursor.json').write_text(json.dumps({'B': {'offset': 8, 'seen': ['old'], 'done': True}}))
    assert len(list(crawler(tmp_path, monkeypatch).crawl('B'))) == 3


def test_same_title_different_articles_never_overwrite(tmp_path):
    job = ArchiveJob(engine(), tmp_path)
    for mid, body in [('1', '正文一'), ('2', '正文二')]:
        job._save(ArticleRef(title='同标题', biz='B', mid=mid, idx='1'),
                  Article(title='同标题', account='A', markdown=body), ArchiveStats())
    files = list(tmp_path.rglob('*.md'))
    assert len(files) == 2
    assert any('正文一' in p.read_text() for p in files)
    assert any('正文二' in p.read_text() for p in files)


def test_text_mode_same_title_and_raw_are_unique(tmp_path):
    run = TextRun(engine(), tmp_path, save_raw=True)
    for mid in ['1', '2']:
        run.save(Article(title='T', account='A', biz='B', mid=mid, idx='1', markdown=mid, content_html=mid), f'https://mp.weixin.qq.com/s/{mid}')
    assert len(list(tmp_path.rglob('*.md'))) == 2
    assert len(list((tmp_path / 'raw').rglob('*.html'))) == 2


def test_image_files_do_not_overwrite_between_articles(tmp_path, monkeypatch):
    real_client = httpx.Client
    monkeypatch.setattr('wxrc.crawl.httpx.Client', lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=str(request.url).encode())), **kwargs))
    a = download_images(Article(images=['https://example.invalid/a']), tmp_path)
    b = download_images(Article(images=['https://example.invalid/b']), tmp_path)
    assert a['https://example.invalid/a'] != b['https://example.invalid/b']
    assert len(list(tmp_path.glob('*.jpg'))) == 2


def test_admin_date_filter_continues_past_newer_page(monkeypatch):
    admin = AdminClient(engine(), AdminSession(cookie='synthetic', token='synthetic'))
    calls = []
    def fetch(begin):
        calls.append(begin)
        return '', 0, 20, [AdminItem(appmsgid=str(begin), publish_ts=200 if begin == 0 else 100)]
    monkeypatch.setattr(admin, 'fetch_page', fetch)
    got = list(admin.iter_published(until=150))
    assert calls == [0, 10] and len(got) == 1 and got[0].publish_ts == 100


@pytest.mark.parametrize('status', [500, 502, 503, 504])
def test_server_errors_are_soft_even_with_json(status):
    assert classify_html('error', status=status).risk is Risk.SOFT
    e = engine(Client('{"ret":0}', status=status), max_attempts=1)
    assert e.get('https://example.invalid', expect='json', raise_on_exhaust=False).verdict.risk is Risk.SOFT


def test_article_and_scripts_do_not_trigger_keyword_false_positive():
    assert classify_html('<div id="js_content"><p>环境异常、去验证和登录超时的处理方式</p></div>').ok
    assert classify_html('<script>const tip="环境异常"</script><p>正常页面</p>').ok
    assert classify_html('<p>环境异常，请完成验证后即可继续访问</p>').risk is Risk.CAPTCHA


def test_parser_preserves_text_around_nested_blocks():
    html = '<div>开头<p>中间<strong>加粗</strong></p>结尾<br>下一行</div>'
    plain, md = text_of(html), html_to_markdown(html)
    for value in ['开头', '中间', '加粗', '结尾', '下一行']:
        assert value in plain and value in md
    assert plain.count('中间') == 1 and '**加粗**' in md


def test_list_request_uses_same_account_headers_limits_and_quota():
    client = Client(json.dumps(list_page(1).payload))
    acc = Account(name='a', uin='synthetic-uin', key='synthetic-key', rpm=1)
    e = engine(client, accounts=[acc])
    ProfileCrawler(e)._fetch_page('B', 0)
    kwargs = client.calls[0][2]
    assert kwargs['headers']['X-WECHAT-KEY'] == kwargs['params']['key'] == acc.key
    assert kwargs['headers']['X-WECHAT-UIN'] == kwargs['params']['uin'] == acc.uin
    assert acc.last_issue > 0 and acc.used_hour == 1


def test_metrics_charge_each_actual_request_and_keep_identity():
    client = Client('{"ret":0,"appmsgstat":{"read_num":10},"elected_comment":[]}')
    acc = Account(name='a', uin='synthetic', key='synthetic-key', rpm=6)
    e = engine(client, accounts=[acc])
    got = MetricsClient(e).fetch_all(biz='B', mid='1', idx='1', sn='x', comment_id='comment')
    assert got['stats'].read_num == 10 and len(client.calls) == 2
    assert acc.used_hour == 2
    assert client.calls[0][2]['headers']['User-Agent'] == client.calls[1][2]['headers']['User-Agent']


def test_list_ret_minus_six_applies_hard_cooldown():
    client = Client('{"ret":-6,"errmsg":"blocked"}')
    acc = Account(name='a', uin='synthetic', key='synthetic')
    e = engine(client, accounts=[acc])
    before = time.time()
    with pytest.raises(NoIdentity):
        ProfileCrawler(e)._fetch_page('B', 0)
    assert len(client.calls) == 1 and acc.cool_until >= before + 2600


def test_nested_error_uses_same_credential_policy():
    assert classify_json({'base_resp': {'ret': 200003, 'errmsg': 'expired'}}).risk is Risk.CRED


def test_breaker_stops_inside_retry_loop():
    client = Client('temporary server error', status=500)
    e = engine(client, breaker=CircuitBreaker(threshold=1, open_seconds=60), max_attempts=3)
    with pytest.raises(BreakerOpen):
        e.get('https://example.invalid')
    assert len(client.calls) == 1


@pytest.mark.parametrize('use_account', [False, True])
def test_no_implicit_direct_when_configured_proxy_unavailable(use_account):
    client = Client()
    node = ProxyNode(url='http://127.0.0.1:9', cool_until=time.time() + 1000)
    acc = Account(name='a') if use_account else None
    e = engine(client, proxy_pool=ProxyPool(nodes=[node]))
    with pytest.raises(NoIdentity):
        e.get('https://example.invalid', account=acc)
    assert not client.calls


def test_browser_cannot_bypass_configured_proxy():
    seen = []
    node = ProxyNode(url='http://127.0.0.1:9')
    e = engine(Client('<p>环境异常</p>'), proxy_pool=ProxyPool(nodes=[node]), max_attempts=1,
               browser_hook=lambda *args: seen.append(args), captcha_browser_threshold=1)
    e.get('https://example.invalid', raise_on_exhaust=False)
    assert not seen


def test_private_session_file_and_redacted_log(tmp_path):
    path = tmp_path / 'session.json'
    path.write_text('old')
    path.chmod(0o644)
    AdminSession(cookie='synthetic-cookie', token='synthetic-token').save(path)
    if os.name != 'nt':
        assert path.stat().st_mode & 0o777 == 0o600
    assert AdminSession.load(path).cookie == 'synthetic-cookie'
    assert 'synthetic-token' not in redact('token=synthetic-token&key=synthetic-key')
    assert 'user:password' not in redact('http://user:password@localhost:9')


def test_skill_entry_works_from_other_directory(tmp_path):
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'shiwen.py'
    result = subprocess.run([sys.executable, str(script), '--version'], cwd=tmp_path, text=True, capture_output=True)
    assert result.returncode == 0 and '零一拾文' in result.stdout and '0.1.8' in result.stdout
    html = tmp_path / 'article.html'
    html.write_text('<h1 id="activity-name">离线文章</h1><div id="js_content">正文</div>')
    parsed = subprocess.run([sys.executable, str(script), 'parse', '--file', str(html)], cwd=tmp_path, text=True, capture_output=True)
    assert parsed.returncode == 0 and json.loads(parsed.stdout)['content_text'] == '正文'


def test_jsonl_recovers_after_truncated_record(tmp_path):
    from wxrc.storage import append_jsonl
    index = tmp_path / 'index.jsonl'
    index.write_bytes(b'{"interrupted":')
    append_jsonl(index, {'key': 'saved'})
    assert json.loads(index.read_text().splitlines()[-1]) == {'key': 'saved'}


def test_skill_batch_command_uses_bundled_source(mock, tmp_path):
    state, base = mock
    state.ua_limit = 100
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'shiwen.py'
    urls = tmp_path / 'urls.txt'
    urls.write_text(f'{base}/s/art1\n{base}/s/art2\n')
    out = tmp_path / 'result'
    result = subprocess.run([sys.executable, str(script), 'text', '--urls', str(urls), '--out', str(out), '--raw'],
                            cwd=tmp_path, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    rows = [json.loads(line) for line in (out / 'index.jsonl').read_text().splitlines()]
    assert len(rows) == 2
    assert all((out / row['path']).exists() for row in rows)
    assert len(list((out / 'raw').rglob('*.html'))) == 2
