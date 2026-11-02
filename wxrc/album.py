"""指定合集的分页、持久清单与正文归档；复用请求引擎和 TextRun。"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .reporting import Coverage
from .storage import atomic_write
from .textmode import TextRun

ALBUM_URL = 'https://mp.weixin.qq.com/mp/appmsgalbum'


@dataclass(frozen=True)
class AlbumTarget:
    biz: str
    album_id: str

    def __post_init__(self):
        if not self.biz.strip() or not re.fullmatch(r'[0-9]+', self.album_id):
            raise ValueError('需要有效的 __biz 和数字 album_id')

    @classmethod
    def from_url(cls, url: str):
        parsed = urlsplit(html.unescape(url.strip()))
        if parsed.scheme not in ('http', 'https') or parsed.hostname != 'mp.weixin.qq.com' or parsed.path != '/mp/appmsgalbum':
            raise ValueError('请提供公众号合集分享链接（/mp/appmsgalbum），普通文章链接不能唯一指定合集')
        args = parse_qs(parsed.query)
        return cls(args.get('__biz', [''])[0], args.get('album_id', [''])[0])


def parse_album_page(payload: dict) -> tuple[dict, list[dict], bool]:
    if not isinstance(payload, dict):
        raise ValueError('合集响应不是对象')
    base = payload.get('base_resp')
    if not isinstance(base, dict) or str(base.get('ret')) != '0':
        raise ValueError('合集接口没有返回成功状态')
    data = payload.get('getalbum_resp')
    if not isinstance(data, dict) or str(data.get('continue_flag')) not in ('0', '1'):
        raise ValueError('合集响应缺少有效的 getalbum_resp / continue_flag')
    info = data.get('base_info')
    if not isinstance(info, dict):
        raise ValueError('合集响应缺少 base_info')
    raw = data.get('article_list')
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError('合集响应缺少有效文章列表，不能将异常响应视为空合集')
    articles = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError('合集文章条目格式无效')
        mid, idx = str(item.get('msgid', '')), str(item.get('itemidx', ''))
        url = html.unescape(str(item.get('url') or ''))
        parsed = urlsplit(url)
        if not mid.isdigit() or not idx.isdigit() or parsed.hostname != 'mp.weixin.qq.com' or parsed.scheme not in ('http', 'https'):
            raise ValueError('合集文章缺少有效标识或微信文章链接')
        articles.append({'key': f'{mid}:{idx}', 'msgid': mid, 'itemidx': idx,
                         'title': str(item.get('title') or ''), 'url': url,
                         'create_time': str(item.get('create_time') or ''), 'status': 'pending', 'error': ''})
    return info, articles, str(data['continue_flag']) == '1'


class AlbumJob:
    def __init__(self, engine, out_dir, target: AlbumTarget, *, save_raw=False, expected_title='', endpoint=ALBUM_URL):
        self.engine, self.target, self.endpoint = engine, target, endpoint
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.out_dir / 'album-state.json'
        self.text = TextRun(engine, self.out_dir, save_raw=save_raw)
        self.expected_title = expected_title
        self.identity = {'biz': target.biz, 'album_id': target.album_id}
        self.state = {'version': 1, 'target': self.identity, 'cursor': ['', ''], 'cursor_history': [],
                      'done': False, 'info': {}, 'articles': {}, 'save_raw': save_raw}
        if self.state_path.exists():
            saved = json.loads(self.state_path.read_text('utf-8'))
            if not isinstance(saved, dict) or saved.get('version') != 1 or saved.get('target') != self.identity:
                raise ValueError('输出目录属于其他合集或状态版本不兼容，请使用独立目录')
            if (not isinstance(saved.get('articles'), dict) or not isinstance(saved.get('info'), dict)
                    or not isinstance(saved.get('cursor'), list) or len(saved['cursor']) != 2
                    or not isinstance(saved.get('cursor_history'), list) or not isinstance(saved.get('done'), bool)):
                raise ValueError('合集断点文件结构损坏，请保留文件并使用新的输出目录')
            self.state = saved
            self.text.save_raw = save_raw or bool(saved.get('save_raw'))
        self.state['save_raw'] = self.text.save_raw
        self.report = Coverage('album_visible_articles')

    def _save(self):
        atomic_write(self.state_path, json.dumps(self.state, ensure_ascii=False, indent=2))

    def run(self, *, max_pages=10000, max_articles=0, list_only=False, restart=False):
        if max_pages < 1 or max_articles < 0:
            raise ValueError('max_pages 必须大于 0，max_articles 不能小于 0（0 表示不限制）')
        if restart:
            self.state.update(cursor=['', ''], cursor_history=[], done=False, info={}, articles={})
        self.report = Coverage('album_visible_articles')
        attempted = set()
        processed = 0
        summary = {}
        self._save()
        try:
            while True:
                info = self.state['info']
                if self.expected_title and info and info.get('title') != self.expected_title:
                    raise ValueError('合集标题与 --expect-title 不匹配，已停止')
                if not list_only:
                    for key, item in self.state['articles'].items():
                        if key in attempted:
                            continue
                        cached = self.text.cached_result(item['url'])
                        if not cached and max_articles and processed >= max_articles:
                            self.report.stop('item_limit')
                            break
                        attempted.add(key)
                        if cached:
                            item.update(status='ok', error='', path=cached.path)
                        else:
                            processed += 1
                            art, error = self.text.fetch(item['url'])
                            if art is None:
                                item.update(status='failed', error=error)
                            else:
                                art.title = art.title or item['title']
                                art.account = art.account or self.state['info'].get('nickname', '')
                                if not art.publish_ts and item['create_time'].isdigit():
                                    art.publish_ts = int(item['create_time'])
                                result = self.text.save(art, item['url'])
                                item.update(status='ok', error='', path=result.path)
                        self._save()
                        if not cached:
                            self.engine.sleep(self.engine.rng.uniform(*self.text.pause))
                    if self.report.stop_reason == 'item_limit':
                        break
                if self.state['done']:
                    reported = str(self.state['info'].get('article_count', ''))
                    mismatch = reported.isdigit() and int(reported) != len(self.state['articles'])
                    self.report.stop('count_mismatch' if mismatch else 'list_end', complete=not mismatch)
                    break
                if self.report.pages >= max_pages:
                    self.report.stop('page_limit')
                    break
                mid, idx = self.state['cursor']
                params = {'action': 'getalbum', '__biz': self.target.biz, 'album_id': self.target.album_id,
                          'count': 20, 'is_reverse': '0', 'f': 'json'}
                if mid:
                    params.update(begin_msgid=mid, begin_itemidx=idx)
                reply = self.engine.get(self.endpoint, params=params, expect='json')
                if not reply.verdict.ok:
                    raise ValueError('合集请求未成功，保留断点后停止')
                info, articles, more = parse_album_page(reply.json())
                self.report.pages += 1
                if self.expected_title and info.get('title') != self.expected_title:
                    raise ValueError('合集标题与 --expect-title 不匹配，已停止')
                # 保存整页清单与下一页游标后才消费正文；中断可安全重放。
                for item in articles:
                    self.state['articles'].setdefault(item['key'], item)
                self.state['info'] = info
                if not more:
                    self.state['done'] = True
                else:
                    next_cursor = [articles[-1]['msgid'], articles[-1]['itemidx']] if articles else None
                    if next_cursor is None or next_cursor == self.state['cursor'] or next_cursor in self.state['cursor_history']:
                        self.report.stop('cursor_stalled')
                        self._save()
                        break
                    self.state['cursor_history'].append(self.state['cursor'])
                    self.state['cursor'] = next_cursor
                self._save()
        except BaseException as exc:
            self.report.interrupt(exc)
            raise
        finally:
            rows = list(self.state['articles'].values())
            ok = sum(i['status'] == 'ok' for i in rows)
            failed = sum(i['status'] == 'failed' for i in rows)
            summary.update(mode='album', target=self.identity, title=self.state['info'].get('title', ''),
                           reported_total=self.state['info'].get('article_count'), listed=len(rows),
                           archived=ok, failed=failed, pending=len(rows)-ok-failed,
                           list_only=list_only, coverage=self.report.as_dict(),
                           archive_complete=not list_only and self.report.coverage_complete and ok == len(rows))
            atomic_write(self.out_dir / 'collection-report.json', json.dumps(summary, ensure_ascii=False, indent=2))
            atomic_write(self.out_dir / 'album-index.json', json.dumps(rows, ensure_ascii=False, indent=2))
            lines = ['# 合集文章目录', '', f'合集 ID：{self.target.album_id}', '']
            for i, row in enumerate(rows, 1):
                title = row['title'].replace('[', '\\[').replace(']', '\\]').replace('\n', ' ')
                lines.append(f"{i}. [{title or row['key']}]({row['url']}) · {row['status']}")
            atomic_write(self.out_dir / '目录.md', '\n'.join(lines)+'\n')
        return summary
