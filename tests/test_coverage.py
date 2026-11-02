import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from wxrc.getmsg import ProfileCrawler, Cursor, ArticleRef
from wxrc.crawl import ArchiveJob, ArchiveStats
from wxrc.article import Article, parse_article
from wxrc.guard import classify_html, Risk
from wxrc.mp_admin import AdminClient, AdminItem, AdminSession
from wxrc.search import SearchRun
from wxrc.session import Reply, BreakerOpen


def engine():
    return SimpleNamespace(sleep=lambda _:None, rng=SimpleNamespace(uniform=lambda *a:0),log=lambda _:None)


def page(mids, more=False):
    return Reply(200,'','','',payload={'ret':0,'next_offset':8,'can_msg_continue':int(more),
        'general_msg_list':{'list':[{'comm_msg_info':{'datetime':100},'app_msg_ext_info':{
        'title':i,'content_url':f'https://example.invalid/s?__biz=B&mid={i}&idx=1'}} for i in mids]}})


@pytest.mark.parametrize('limit,max_pages,more,reason,complete',[(1,2,True,'item_limit',False),(10,1,True,'page_limit',False),(10,1,False,'list_end',True)])
def test_scan_boundaries(monkeypatch,limit,max_pages,more,reason,complete):
    c=ProfileCrawler(engine())
    monkeypatch.setattr(c,'_fetch_page',lambda *a:page(['a','b'],more))
    list(c.crawl('B',limit=limit,max_pages=max_pages))
    assert c.report.stop_reason==reason and c.report.coverage_complete==complete


def test_date_boundary(monkeypatch):
    c=ProfileCrawler(engine())
    monkeypatch.setattr(c,'_fetch_page',lambda *a:page(['old'],True))
    assert not list(c.crawl('B',since=200))
    assert c.report.stop_reason=='date_boundary' and c.report.coverage_complete


def test_update_keeps_history_cursor_and_skips_existing(tmp_path,monkeypatch):
    job=ArchiveJob(engine(),tmp_path)
    old=ArticleRef(url='https://example.invalid/s?__biz=B&mid=old&idx=1',biz='B',mid='old',idx='1')
    job._save(old,Article(title='old',markdown='old'),ArchiveStats())
    cursor=tmp_path/'cursor.json'
    cursor.write_text('{"B":{"offset":88,"done":false}}')
    original=cursor.read_bytes()
    monkeypatch.setattr(ProfileCrawler,'_fetch_page',lambda *a:page(['new','old']))
    fetched=[]
    def fetch(ref,stats):
        fetched.append(ref.mid)
        stats.fetched+=1
        return Article(title=ref.title,markdown=ref.title)
    monkeypatch.setattr(job,'fetch_article',fetch)
    stats=job.run('B',update=True)
    assert fetched==['new'] and stats.skipped==1 and cursor.read_bytes()==original
    report=json.loads((tmp_path/'collection-report.json').read_text())
    assert report['mode']=='update' and report['coverage']['coverage_complete']


def test_interrupted_report_persisted(tmp_path,monkeypatch):
    job=ArchiveJob(engine(),tmp_path)
    def fail(*a): raise BreakerOpen(10)
    monkeypatch.setattr(ProfileCrawler,'_fetch_page',fail)
    with pytest.raises(BreakerOpen): job.run('B')
    report=json.loads((tmp_path/'collection-report.json').read_text())
    assert report['coverage']['stop_reason']=='circuit_open' and not report['archive_complete']


def test_admin_limit_and_end(monkeypatch):
    c=AdminClient(engine(),AdminSession())
    monkeypatch.setattr(c,'fetch_page',lambda n:('',0,30,[AdminItem(appmsgid=str(n),publish_ts=100)]))
    list(c.iter_published(max_pages=1))
    assert c.report.stop_reason=='page_limit' and not c.report.coverage_complete
    list(c.iter_published(max_pages=3))
    assert c.report.stop_reason=='list_end' and c.report.coverage_complete


def test_search_always_declares_sample():
    assert SearchRun().summary()['coverage']['scope']=='search_sample'
    assert not SearchRun().summary()['coverage']['coverage_complete']


FIXTURES=Path(__file__).parent/'fixtures'/'articles'


def test_structured_article_fixture():
    art=parse_article((FIXTURES/'structured.html').read_text())
    md=art.markdown
    assert '| 名称 | 值 |' in md and '| A\\|B | 一  <br>二 |' in md
    assert '3. 主项\n   - 子项\n4. 次项' in md
    assert '````\nfirst\n\n\n```inside\nlast\n````' in md
    assert md.index('图前')<md.index('![](https://example.invalid/pic.png)')<md.index('图后')
    assert '段前强调段后' in art.content_text


def test_deleted_and_image_only_fixtures():
    assert classify_html((FIXTURES/'deleted.html').read_text()).risk is Risk.GONE
    art=parse_article((FIXTURES/'image-only.html').read_text())
    assert not art.deleted and '![](https://example.invalid/only.png)' in art.markdown


def test_dry_run_does_not_advance_saved_cursor(tmp_path,monkeypatch):
    from wxrc import cli
    sentinel=tmp_path/'cursor.json'
    sentinel.write_text('{"existing":true}')
    monkeypatch.setattr(cli,'build_engine',lambda cfg:engine())
    monkeypatch.setattr(ProfileCrawler,'_fetch_page',lambda *a:page(['new']))
    assert cli.main(['crawl','--biz','B','--dry-run','--out',str(tmp_path)])==0
    assert sentinel.read_text()=='{"existing":true}'


def test_complete_scan_with_failed_body_is_not_complete_archive(tmp_path,monkeypatch):
    job=ArchiveJob(engine(),tmp_path)
    monkeypatch.setattr(ProfileCrawler,'_fetch_page',lambda *a:page(['x']))
    def fail(ref,stats):
        stats.failed+=1
        return None
    monkeypatch.setattr(job,'fetch_article',fail)
    job.run('B')
    report=json.loads((tmp_path/'collection-report.json').read_text())
    assert report['coverage']['coverage_complete'] and not report['archive_complete']


def test_nested_list_tail_stays_after_child():
    from wxrc.article import html_to_markdown
    md=html_to_markdown('<ol><li>before<ul><li>child</li></ul>after</li></ol>')
    assert md.index('before')<md.index('child')<md.index('after')


def test_missing_article_url_counts_as_failure(tmp_path):
    stats=ArchiveStats()
    assert ArchiveJob(engine(),tmp_path).fetch_article(ArticleRef(title='missing URL'),stats) is None
    assert stats.failed==1
