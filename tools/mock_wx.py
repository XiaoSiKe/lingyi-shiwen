"""本地风控模拟服务：复现 mp.weixin.qq.com 的三类拦截，用于离线跑通轮换逻辑。

真机规则（2026-10 实测归纳）：
- 列表接口 `ret=-3` 频率、`ret=-6` 行为风控、`ret=200003` key 失效；
- 文章页对「非客户端 UA」直接 302 到 /mp/verify，返回「环境异常」；
- 同一 UA 或同一出口短时间内连续请求，也会被推到验证页。

这里把它参数化成阈值，方便测试不同轮换策略。
"""
from __future__ import annotations

import json
import threading
import html as _html
import json as _json
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

VERIFY_HTML = """<html><head><title>环境异常</title></head><body>
<div class="weui-msg"><h2>环境异常</h2>
<p>当前环境异常，完成验证后即可继续访问。</p><a href="/mp/verify">去验证</a></div>
</body></html>"""

PLAIN_HTML = """<html><head><title>wxrc kernel probe</title></head><body>
<h1 id="plain">wxrc kernel probe</h1>
<p>指纹内核连通性探针页</p>
</body></html>"""

ARTICLE_HTML = """<html><head>
<meta property="og:title" content="{title}">
<meta property="og:image" content="https://mmbiz.qpic.cn/cover/0?wx_fmt=jpeg">
<meta property="og:description" content="{digest}">
</head><body>
<h1 id="activity-name">{title}</h1>
<a id="js_name">{account}</a>
<em id="publish_time">2026年10月{day}日 08:00</em>
<div id="js_content">
  <section><p>第一段：{title} 的正文开始。</p>
    <p><img data-src="https://mmbiz.qpic.cn/mmbiz_jpg/abc{i}/0?wx_fmt=jpeg"></p>
  </section>
  <section><p>第二段：这里放一段足够长的正文，让解析器有东西可抽。</p></section>
</div>
<script>
var msg_title = "{title}";
var nickname = "{account}";
var ct = "{ct}";
var biz = "MZTESTBIZ==";
var appmsgid = "26575{i}";
var idx = "1";
var sn = "sn{i}";
var comment_id = "3945457940655423488";
var comment_enabled = "1" * 1;
var msg_desc = "{digest}";
</script>
</body></html>"""


class MockState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.key_hits: dict[str, int] = {}
        self.ua_hits: dict[str, int] = {}
        self.dead_keys: set[str] = set()
        self.banned_uins: set[str] = set()
        self.force_captcha_paths: set[str] = set()
        self.session_keys: set[str] = {"k1", "k2"}   # 有会话的 key（getappmsgext / 评论可用）
        self.read_num = 12345
        self.like_num = 88
        self.admin_token = "tok123"
        self.admin_items = 25
        self.admin_rate_limit = False
        self.key_limit = 3
        self.ua_limit = 2
        self.requests = 0

    def hit_key(self, key: str) -> int:
        with self.lock:
            self.key_hits[key] = self.key_hits.get(key, 0) + 1
            return self.key_hits[key]

    def hit_ua(self, ua: str) -> int:
        with self.lock:
            self.ua_hits[ua] = self.ua_hits.get(ua, 0) + 1
            return self.ua_hits[ua]


def make_handler(state: MockState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 静音
            return

        # ---------------------------------------------------------- #
        def do_GET(self):
            state.requests += 1
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            if path == "/mp/profile_ext":
                return self._profile_ext(qs)
            if path == "/mp/getappmsgext":
                return self._getappmsgext(qs)
            if path == "/mp/appmsg_comment":
                return self._comment(qs)
            if path == "/mp/verify":
                return self._send(200, VERIFY_HTML, "text/html; charset=utf-8")
            if path.startswith("/s/"):
                return self._article(path, qs)
            if path == "/cgi-bin/appmsgpublish":
                return self._appmsgpublish(qs)
            if path == "/weixin":
                return self._search_page(qs)
            if path == "/link":
                return self._hop(qs)
            if path == "/plain":
                return self._send(200, PLAIN_HTML, "text/html; charset=utf-8")
            return self._send(404, "not found", "text/plain")

        # ---------------------------------------------------------- #
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            qs = parse_qs(body)
            path = urlparse(self.path).path
            if path == "/mp/getappmsgext":
                return self._getappmsgext(qs)
            if path == "/mp/appmsg_comment":
                return self._comment(qs)
            return self._send(404, "not found", "text/plain")

        # ---------------------------------------------------------- #
        def _profile_ext(self, qs: dict):
            key = (qs.get("key") or [""])[0]
            uin = (qs.get("uin") or [""])[0]
            offset = int((qs.get("offset") or ["0"])[0] or 0)
            if key in state.dead_keys:
                return self._json({"ret": 200003, "errmsg": "invalid key"})
            if uin in state.banned_uins:
                return self._json({"ret": -6, "errmsg": "operate too frequent"})
            if state.hit_key(key) > state.key_limit:
                return self._json({"ret": -3, "errmsg": "freq control"})

            total = 16
            page = [i for i in range(offset, min(offset + 8, total))]
            items = [self._msg(i) for i in page]
            payload = {
                "ret": 0,
                "errmsg": "ok",
                "next_offset": offset + len(page),
                "can_msg_continue": 1 if offset + len(page) < total else 0,
                "general_msg_list": json.dumps({"list": items}, ensure_ascii=False),
            }
            return self._json(payload)

        def _msg(self, i: int) -> dict:
            host = f"http://{self.headers.get('Host')}"
            return {
                "comm_msg_info": {"id": 1000 + i, "type": 49, "datetime": 1750000000 + i * 86400},
                "app_msg_ext_info": {
                    "title": f"测试文章 {i}",
                    "digest": f"摘要 {i}",
                    "content_url": f"{host}/s/art{i}?__biz=MZTESTBIZ==&mid=26575{i}&idx=1&sn=sn{i}",
                    "cover": f"https://mmbiz.qpic.cn/cover/{i}?wx_fmt=jpeg",
                    "author": "作者",
                    "itemidx": 1,
                },
            }

        def _appmsgpublish(self, qs: dict):
            """模拟后台发表记录：publish_page 是 JSON 字符串，publish_info 是 HTML 实体编码的 JSON。"""
            if state.admin_rate_limit:
                return self._json({"base_resp": {"ret": 200013, "errmsg": "freq limit"}})
            token = (qs.get("token") or [""])[0]
            cookie = self.headers.get("Cookie", "")
            if token != state.admin_token or "slave_sid=" not in cookie:
                return self._json({"base_resp": {"ret": 200003, "errmsg": "invalid session"}})

            begin = int((qs.get("begin") or ["0"])[0] or 0)
            count = int((qs.get("count") or ["10"])[0] or 10)
            host = f"http://{self.headers.get('Host')}"
            base_day = datetime(2025, 6, 28)
            rows = []
            for i in range(begin, min(begin + count, state.admin_items)):
                day = (base_day - timedelta(days=i)).strftime("%Y-%m-%d")
                msg = {
                    "title": f"后台文章 {i}",
                    "content_url": f"{host}/s/art{i % 16}?__biz=MZTESTBIZ==&mid=2657{i:03d}&idx=1&sn=sn{i}",
                    "line_info": {"send_time": f"{day} 08:00"},
                    "appmsgid": f"2657{i:03d}",
                    "itemidx": "1",
                    "read_num": 1000 + i,
                    "like_num": i,
                    "old_like_num": i // 2,
                    "comment_count": 3,
                    "digest": f"摘要 {i}",
                }
                info = {"appmsg_info": [msg]}
                rows.append({"publish_info": _html.escape(_json.dumps(info, ensure_ascii=False))})
            page = {"publish_list": rows, "total_count": state.admin_items}
            return self._json({"base_resp": {"ret": 0}, "publish_page": _json.dumps(page, ensure_ascii=False)})

        def _search_page(self, qs: dict):
            """模拟搜狗文章搜索：只给 /link?url= 形式的结果，第 2 页起为空。"""
            page = int((qs.get("page") or ["1"])[0] or 1)
            base = f"http://{self.headers.get('Host')}"
            if page != 1:
                return self._send(200, "<html><body>没有找到相关结果</body></html>", "text/html; charset=utf-8")
            targets = [
                f"{base}/s/art3?__biz=MZTESTBIZ==&mid=265753&idx=1&sn=sn3",
                f"{base}/s/other?acct=%E5%88%AB%E7%9A%84%E5%85%AC%E4%BC%97%E5%8F%B7",
                f"{base}/s/art3?__biz=MZTESTBIZ==&mid=265753&idx=1&sn=sn3",   # 重复项，测去重
            ]
            items = "".join(
                f'<li><h3><a href="/link?url={quote(t, safe="")}">文章</a></h3></li>' for t in targets
            )
            html = f"<html><body><ul class='news-list'>{items}</ul></body></html>"
            return self._send(200, html, "text/html; charset=utf-8")

        def _hop(self, qs: dict):
            target = (qs.get("url") or [""])[0]
            js = (
                "<meta content='always' name='referrer'><script>"
                "var url='';"
                f"url += '{target}';"
                "setTimeout(function(){window.location.replace(url);}, 100);"
                "</script>"
            )
            return self._send(200, js, "text/html; charset=utf-8")

        def _article(self, path: str, qs: dict | None = None):
            qs = qs or {}
            account = (qs.get("acct") or ["测试公众号"])[0] or "测试公众号"
            slug = path.rsplit("/", 1)[-1]
            if path in state.force_captcha_paths:
                return self._redirect("/mp/verify?from=antispider")
            ua = self.headers.get("User-Agent", "")
            if "browser-only" in path and not self.headers.get("sec-fetch-user"):
                # 只放行真实浏览器导航（Sec-Fetch-User 是浏览器独有头）
                return self._redirect("/mp/verify?from=ua")
            if "mp-only" in path:
                # 严格模式：只认微信客户端 UA，用来验证「换指纹」这一层
                if "MicroMessenger" not in ua:
                    return self._redirect("/mp/verify?from=ua")
            elif "MicroMessenger" not in ua and "Chrome" not in ua and "Safari" not in ua:
                return self._redirect("/mp/verify?from=ua")   # 工具 UA 一律拦
            if state.hit_ua(ua) > state.ua_limit:
                return self._redirect("/mp/verify?from=freq")  # 同 UA 高频
            i = "".join(ch for ch in slug if ch.isdigit()) or "0"
            html = ARTICLE_HTML.format(
                title=f"测试文章 {i}",
                account=account,
                digest=f"摘要 {i}",
                day=f"{(int(i) % 28) + 1:02d}",
                ct=str(1750000000 + int(i) * 86400),
                i=i,
            )
            return self._send(200, html, "text/html; charset=utf-8")

        def _getappmsgext(self, qs: dict):
            """真机行为：有会话给 appmsgstat；无会话 200 但**没有** appmsgstat。"""
            key = (qs.get("key") or [""])[0]
            if key in state.session_keys:
                return self._json(
                    {
                        "base_resp": {"ret": 0},
                        "appmsgstat": {
                            "read_num": state.read_num,
                            "like_num": state.like_num,
                            "old_like_num": 77,
                        },
                        "comment_count": 3,
                    }
                )
            return self._json({"base_resp": {"ret": 0}, "advertisement_info": [], "more_read_list": []})

        def _comment(self, qs: dict):
            key = (qs.get("key") or [""])[0]
            if key not in state.session_keys:
                return self._json(
                    {"ret": -3, "errmsg": "no session", "base_resp": {"ret": -3, "errmsg": "no session"}, "cookie_count": 0}
                )
            return self._json(
                {
                    "ret": 0,
                    "elected_comment_total_cnt": 2,
                    "elected_comment": [
                        {
                            "content": "第一条留言：写得很清楚",
                            "create_time": 1750000000,
                            "like_num": 5,
                            "nick_name": "读者A",
                            "reply": {"content": "谢谢支持", "nick_name": "作者"},
                        },
                        {
                            "content": "第二条留言",
                            "create_time": 1750086400,
                            "like_num": 1,
                            "nick_name": "读者B",
                        },
                    ],
                }
            )

        # ---------------------------------------------------------- #
        def _json(self, obj: dict):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send(self, status: int, text: str, ctype: str):
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location: str):
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


def start(port: int = 0) -> tuple[ThreadingHTTPServer, MockState, str]:
    state = MockState()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    return server, state, base


if __name__ == "__main__":
    import time

    srv, st, base = start(8123)
    print(f"mock wx listening on {base}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.shutdown()
