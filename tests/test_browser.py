"""标准浏览器适配层契约：不需要真实账号或浏览器即可验证资源与凭证处理。"""
import sys
from contextlib import contextmanager
from types import SimpleNamespace, ModuleType

import pytest

from wxrc.browser import fetch_via_browser, make_browser_hook
from wxrc.mp_admin import AdminSession, login_via_browser


@pytest.fixture
def browser_stub(monkeypatch):
    calls = {}
    page = SimpleNamespace(url='https://mp.weixin.qq.com/?token=123',
        goto=lambda *a, **kw: SimpleNamespace(status=200),
        content=lambda: '<div id="js_content">正文</div>',
        evaluate=lambda expression: 'synthetic-agent', wait_for_timeout=lambda ms: None)
    ctx = SimpleNamespace(pages=[page], cookies=lambda urls: [{'name':'slave_sid','value':'synthetic-cookie'}],
                          close=lambda: calls.update(closed=True))
    def launch(*args, **kwargs):
        calls['launch'] = kwargs
        return ctx
    @contextmanager
    def sync():
        yield SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    module = ModuleType('playwright.sync_api')
    module.sync_playwright = sync
    monkeypatch.setitem(sys.modules,'playwright.sync_api',module)
    monkeypatch.setattr('wxrc.browser.playwright_available',lambda: True)
    return page, calls


def test_admin_login_saves_session_and_closes(browser_stub, monkeypatch, tmp_path):
    page, calls = browser_stub
    monkeypatch.setattr('wxrc.browser.profile_directory',lambda key: tmp_path/'profile')
    logs=[]
    path=tmp_path/'admin.json'
    session=login_via_browser(save_path=path,logger=logs.append)
    assert session.token == '123' and AdminSession.load(path).cookie == 'slave_sid=synthetic-cookie'
    assert calls['closed'] and calls['launch']['headless'] is False
    assert 'synthetic-cookie' not in ''.join(logs) and '123' not in ''.join(logs)


def test_login_timeout_closes(browser_stub, monkeypatch, tmp_path):
    page,calls=browser_stub
    monkeypatch.setattr('wxrc.browser.profile_directory',lambda key: tmp_path/'profile')
    assert login_via_browser(timeout_s=0) is None
    assert calls['closed']


def test_navigation_failure_does_not_become_success(browser_stub, tmp_path):
    page,calls=browser_stub
    def fail(*a,**kw): raise RuntimeError('navigation failed')
    page.goto=fail
    assert fetch_via_browser('https://example.invalid',profile_dir=tmp_path) is None
    assert calls['closed']


def test_proxy_forwarded_and_normal_article_accepted(browser_stub,tmp_path):
    page,calls=browser_stub
    page.content=lambda: '<div id="js_content">环境异常处理教程</div>'
    hook=make_browser_hook('playwright',data_root=tmp_path)
    result=hook('https://example.invalid','',proxy_url='http://u:p@localhost:8080')
    assert result[0] == 200
    assert calls['launch']['proxy'] == {'server':'http://localhost:8080','username':'u','password':'p'}
    assert calls['closed']


def test_unknown_engine_fails_explicitly():
    with pytest.raises(ValueError): make_browser_hook('invalid-engine')
