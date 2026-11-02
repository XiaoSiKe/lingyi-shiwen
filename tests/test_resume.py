import json
from types import SimpleNamespace
import pytest
from wxrc.article import Article
from wxrc.session import BreakerOpen
from wxrc.storage import output_lock
from wxrc.textmode import TextRun


def runner(tmp_path, **kwargs):
    engine=SimpleNamespace(sleep=lambda s:None, rng=SimpleNamespace(uniform=lambda *a:0))
    return TextRun(engine,tmp_path,**kwargs)


def article():
    return Article(title='title',account='account',content_text='body',markdown='body',content_html='<p>body</p>')


def test_restart_skips_only_existing_artifacts(tmp_path, monkeypatch):
    first=runner(tmp_path)
    monkeypatch.setattr(first,'fetch',lambda u:(article(),''))
    first.run(['u','u'])
    assert len(first.results)==1
    second=runner(tmp_path)
    monkeypatch.setattr(second,'fetch',lambda u:pytest.fail('already downloaded'))
    assert second.run(['u'])[0].skipped
    assert len((tmp_path/'index.jsonl').read_text().splitlines())==1
    from pathlib import Path
    Path(second.results[0].path).unlink()
    third=runner(tmp_path)
    monkeypatch.setattr(third,'fetch',lambda u:(article(),''))
    assert not third.run(['u'])[0].skipped


def test_raw_upgrade_and_force_refetch(tmp_path, monkeypatch):
    first=runner(tmp_path)
    first.save(article(),'u')
    raw=runner(tmp_path,save_raw=True)
    assert raw.cached_result('u') is None
    monkeypatch.setattr(raw,'fetch',lambda u:(article(),''))
    raw.run(['u'])
    assert runner(tmp_path,save_raw=True).cached_result('u').skipped
    assert runner(tmp_path,force=True).cached_result('u') is None


def test_failure_and_interruption_resume(tmp_path,monkeypatch):
    first=runner(tmp_path)
    def fetch(u):
        if u=='paused': raise BreakerOpen(60)
        return None,'temporary failure'
    monkeypatch.setattr(first,'fetch',fetch)
    with pytest.raises(BreakerOpen):first.run(['failed','paused','later'])
    report=json.loads(first.report_path.read_text())
    assert report['state']=='interrupted' and report['pending']==['paused','later']
    second=runner(tmp_path)
    assert second.retry_urls()==['failed','paused','later']
    monkeypatch.setattr(second,'fetch',lambda u:(article(),''))
    second.run(second.retry_urls())
    report=json.loads(second.report_path.read_text())
    assert report['state']=='complete' and report['successful']==3
    assert not second.retry_urls()


def test_output_lock_released_even_on_error(tmp_path):
    with pytest.raises(RuntimeError):
        with output_lock(tmp_path):
            with pytest.raises(ValueError, match='已有任务'):
                with output_lock(tmp_path):pass
            raise RuntimeError('interruption')
    with output_lock(tmp_path):pass


def test_admin_empty_body_reports_failure(tmp_path,monkeypatch,capsys):
    from wxrc import cli
    from wxrc.mp_admin import AdminItem, AdminSession
    path=tmp_path/'session.json'
    AdminSession(cookie='synthetic',token='synthetic').save(path)
    item=AdminItem(title='article',url='https://example.invalid')
    monkeypatch.setattr(cli,'_bare_engine',lambda:None)
    monkeypatch.setattr('wxrc.mp_admin.AdminClient',lambda *a:SimpleNamespace(iter_published=lambda **kw:iter([item])))
    monkeypatch.setattr(TextRun,'fetch',lambda *a:(None,'empty body'))
    assert cli.main(['admin','--session',str(path),'--out',str(tmp_path/'out'),'--text'])==1
    assert '"body_failed": 1' in capsys.readouterr().out
    assert not list((tmp_path/'out').rglob('*.md'))


def test_retry_completed_task_is_success(tmp_path,monkeypatch):
    from wxrc import cli
    run=runner(tmp_path)
    monkeypatch.setattr(run,'fetch',lambda u:(article(),''))
    run.run(['u'])
    monkeypatch.setattr(cli,'_bare_engine',lambda:None)
    assert cli.main(['text','--out',str(tmp_path),'--retry-failed'])==0
