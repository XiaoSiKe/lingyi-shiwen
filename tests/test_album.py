"""离线合集契约测试：所有请求由假引擎接收，不连接微信。"""
import json
from types import SimpleNamespace
import pytest
from wxrc.album import AlbumJob, AlbumTarget, parse_album_page
from wxrc.article import Article
from wxrc.session import Reply, BreakerOpen

TARGET=AlbumTarget('B==','123')
URL='https://mp.weixin.qq.com/mp/appmsgalbum?__biz=B%3D%3D&album_id=123'


def payload(ids,more=False,total=3):
    return {'base_resp':{'ret':0},'getalbum_resp':{'base_info':{'title':'零一文章','nickname':'测试公众号','article_count':str(total)},
        'continue_flag':str(int(more)), 'article_list':[{'msgid':str(i),'itemidx':'1','title':f'article {i}',
        'url':f'https://mp.weixin.qq.com/s/{i}','create_time':'1700000000'} for i in ids]}}


class Engine:
    def __init__(self,pages):
        self.pages=iter(pages);self.calls=[]
        self.sleep=lambda _:None
        self.rng=SimpleNamespace(uniform=lambda *a:0)
    def get(self,url,**kwargs):
        self.calls.append((url,kwargs))
        body=next(self.pages)
        if isinstance(body,Exception):raise body
        return Reply(200,'',url,url,payload=body)


def job(tmp_path,monkeypatch,pages):
    e=Engine(pages)
    j=AlbumJob(e,tmp_path,TARGET)
    monkeypatch.setattr(j.text,'fetch',lambda url:(Article(url=url,title=url[-1],markdown='正文'),''))
    return j,e


def test_multi_page_dedup_and_cursor(tmp_path,monkeypatch):
    j,e=job(tmp_path,monkeypatch,[payload([1,2],True),payload([2,3])])
    result=j.run()
    assert result['archive_complete'] and result['listed']==3 and result['archived']==3
    assert e.calls[1][1]['params']['begin_msgid']=='2'
    assert e.calls[1][1]['params']['begin_itemidx']=='1'
    assert len(list(tmp_path.glob('**/*.md')))==4  # 三篇正文与目录
    assert len(json.loads((tmp_path/'album-index.json').read_text()))==3


def test_page_limit_resume(tmp_path,monkeypatch):
    j,e=job(tmp_path,monkeypatch,[payload([1,2],True)])
    assert j.run(max_pages=1)['coverage']['stop_reason']=='page_limit'
    j,e=job(tmp_path,monkeypatch,[payload([3])])
    assert j.run()['archive_complete']
    assert e.calls[0][1]['params']['begin_msgid']=='2'


def test_article_limit_makes_progress_across_runs(tmp_path,monkeypatch):
    j,e=job(tmp_path,monkeypatch,[payload([1,2,3])])
    assert j.run(max_articles=1)['archived']==1
    j,e=job(tmp_path,monkeypatch,[])
    assert j.run(max_articles=1)['archived']==2
    j,e=job(tmp_path,monkeypatch,[])
    assert j.run(max_articles=1)['archive_complete'] and not e.calls


def test_list_only_then_archive(tmp_path,monkeypatch):
    j,e=job(tmp_path,monkeypatch,[payload([1,2,3])])
    result=j.run(list_only=True)
    assert result['coverage']['coverage_complete'] and not result['archive_complete']
    assert result['pending']==3
    j,e=job(tmp_path,monkeypatch,[])
    assert j.run()['archive_complete'] and not e.calls


def test_interruption_preserves_page(tmp_path,monkeypatch):
    j,e=job(tmp_path,monkeypatch,[payload([1,2,3])])
    def fail(url):raise BreakerOpen(30)
    monkeypatch.setattr(j.text,'fetch',fail)
    with pytest.raises(BreakerOpen):j.run()
    report=json.loads((tmp_path/'collection-report.json').read_text())
    assert report['pending']==3 and report['coverage']['stop_reason']=='circuit_open'
    j,e=job(tmp_path,monkeypatch,[])
    assert j.run()['archive_complete']


def test_failed_article_retried_after_list_end(tmp_path,monkeypatch):
    j,e=job(tmp_path,monkeypatch,[payload([1],total=1)])
    monkeypatch.setattr(j.text,'fetch',lambda url:(None,'temporary failure'))
    result=j.run()
    assert result['failed']==1 and not result['archive_complete']
    j,e=job(tmp_path,monkeypatch,[])
    assert j.run()['archive_complete']


@pytest.mark.parametrize('pages,reason', [([payload([1],True),payload([1],True)],'cursor_stalled'),([payload([1],total=9)],'count_mismatch')])
def test_incomplete_responses_never_claim_complete(tmp_path,monkeypatch,pages,reason):
    j,e=job(tmp_path,monkeypatch,pages)
    r=j.run()
    assert r['coverage']['stop_reason']==reason and not r['archive_complete']


@pytest.mark.parametrize('body',[{}, {'base_resp':{'ret':0},'getalbum_resp':{}},payload([1])|{'base_resp':{'ret':200003}}])
def test_malformed_response_is_error(body):
    with pytest.raises(ValueError):parse_album_page(body)


def test_target_and_title_guard(tmp_path):
    assert AlbumTarget.from_url(URL)==TARGET
    with pytest.raises(ValueError):AlbumTarget.from_url('https://example.invalid/mp/appmsgalbum?album_id=123&__biz=B')
    j=AlbumJob(Engine([payload([1])]),tmp_path,TARGET,expected_title='另一合集')
    with pytest.raises(ValueError,match='标题'):j.run()
    with pytest.raises(ValueError,match='其他合集'):AlbumJob(Engine([]),tmp_path,AlbumTarget('B','456'))


def test_singleton_article_response():
    body=payload([1],total=1)
    body['getalbum_resp']['article_list']=body['getalbum_resp']['article_list'][0]
    assert len(parse_album_page(body)[1])==1


def test_missing_file_is_repaired(tmp_path,monkeypatch):
    from pathlib import Path
    j,e=job(tmp_path,monkeypatch,[payload([1],total=1)])
    j.run()
    Path(j.state['articles']['1:1']['path']).unlink()
    j,e=job(tmp_path,monkeypatch,[])
    assert j.run()['archive_complete']
    assert Path(j.state['articles']['1:1']['path']).exists()


def test_cli_core_flow_and_resume_without_network(tmp_path,monkeypatch,capsys):
    from wxrc import cli
    class Flow(Engine):
        def get(self,url,**kwargs):
            if '/mp/appmsgalbum' in url:
                return super().get(url,**kwargs)
            self.calls.append((url,kwargs))
            content=f'<h1 id="activity-name">正文标题</h1><div id="js_content"><p>正文内容</p></div>'
            return Reply(200,content,url,url)
    e=Flow([payload([1],total=1)])
    monkeypatch.setattr(cli,'_bare_engine',lambda:e)
    args=['album','--url',URL,'--expect-title','零一文章','--out',str(tmp_path),'--raw']
    assert cli.main(args)==0
    assert len(e.calls)==2
    assert len(list((tmp_path/'raw').rglob('*.html')))==1
    assert cli.main(args)==0 and len(e.calls)==2
    assert '"archive_complete": true' in capsys.readouterr().out


def test_restart_reenumerates_and_reuses_body(tmp_path,monkeypatch):
    j,e=job(tmp_path,monkeypatch,[payload([1],total=1)])
    j.run()
    j,e=job(tmp_path,monkeypatch,[payload([2,1],total=2)])
    calls=[]
    def fetch(url):
        calls.append(url)
        return Article(title='new',markdown='new'),''
    monkeypatch.setattr(j.text,'fetch',fetch)
    result=j.run(restart=True)
    assert result['archive_complete'] and len(calls)==1 and result['listed']==2


def test_empty_album_requires_explicit_end_and_zero_total(tmp_path,monkeypatch):
    j,e=job(tmp_path,monkeypatch,[payload([],total=0)])
    result=j.run()
    assert result['archive_complete'] and result['listed']==0


def test_corrupt_state_is_not_silently_reset(tmp_path):
    path=tmp_path/'album-state.json'
    original=json.dumps({'version':1,'target':{'biz':TARGET.biz,'album_id':TARGET.album_id}})
    path.write_text(original)
    with pytest.raises(ValueError,match='损坏'):AlbumJob(Engine([]),tmp_path,TARGET)
    assert path.read_text()==original
