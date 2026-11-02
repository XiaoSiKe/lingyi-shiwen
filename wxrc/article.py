"""正文层：文章页解析（新旧两套结构都要能吃）。

mp.weixin.qq.com 的文章页有两代模板：
- 旧：`#js_content` + `var ct`/`var msg_title`/`#activity-name`；
- 新：可能只给 `content_noencode`（纯文本）或结构改成 section 嵌套，`#js_content` 仍多在，
  但图片全部走 `data-src`（懒加载），直接取 `src` 会拿到占位图。

另外文章页本身也是风控入口：工具 UA 会被 302 到「环境异常」，
所以这里只负责「拿到 HTML 之后怎么榨干净」，拦截判定一律交给 guard。
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from typing import Iterator
from urllib.parse import parse_qs, urljoin, urlparse

from lxml import html as lxml_html

BLOCK_TAGS = {
    "p", "section", "div", "article", "blockquote", "pre", "li",
    "h1", "h2", "h3", "h4", "h5", "h6", "figure", "figcaption", "td",
}
INLINE_END = {"br"}


@dataclass
class Article:
    url: str = ""
    title: str = ""
    account: str = ""
    author: str = ""
    publish_ts: int = 0
    publish_text: str = ""
    digest: str = ""
    cover: str = ""
    content_html: str = ""
    content_text: str = ""
    markdown: str = ""
    images: list[str] = field(default_factory=list)
    biz: str = ""
    mid: str = ""
    idx: str = ""
    sn: str = ""
    comment_id: str = ""
    comment_enabled: int = 0
    appmsg_token: str = ""
    deleted: bool = False
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d.pop("content_html", None)   # 原始 HTML 单独存盘，不进索引
        return d


# --------------------------------------------------------------------------- #
# JS 变量抽取
# --------------------------------------------------------------------------- #
def js_var(source: str, name: str, default: str = "") -> str:
    """抽 `var name = "..."`（微信把标题/正文/凭证都塞在行内 JS 里）。

    注意 `var biz = "MZ..." || ""` 这种写法：必须停在**匹配的引号**上，
    否则会把后面的 `|| ''` 一起吞进来（列表里就会出现 `biz="MZ...==" || "`）。
    """
    pattern = r"var\s+" + re.escape(name) + r'\s*=\s*"((?:\\.|[^"\\])*)"'
    m = re.search(pattern, source or "")
    if not m:
        pattern = r"var\s+" + re.escape(name) + r"\s*=\s*'((?:\\.|[^'\\])*)'"
        m = re.search(pattern, source or "")
    if not m:
        return default
    val = m.group(1)
    return _html.unescape(val.replace("\\x26", "&").replace("\\/", "/"))


def js_num(source: str, name: str, default: int = 0) -> int:
    m = re.search(r"var\s+" + re.escape(name) + r"\s*=\s*[\"']?(\d{6,})", source or "")
    return int(m.group(1)) if m else default


def _int_var(raw: str, default: int = 0) -> int:
    try:
        return int(str(raw).strip().strip("\"'"))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
def parse_article(raw_html: str, url: str = "") -> Article:
    doc = lxml_html.fromstring(raw_html or "<html></html>")
    art = Article(url=url)

    # ---- 元信息 ---- #
    art.title = _text(doc, "activity-name") or js_var(raw_html, "msg_title") or _meta(doc, "og:title")
    art.account = _text(doc, "js_name") or js_var(raw_html, "nickname")
    art.author = _text(doc, "js_author_name") or js_var(raw_html, "author")
    art.digest = _meta(doc, "og:description") or js_var(raw_html, "msg_desc")
    art.cover = _meta(doc, "og:image") or js_var(raw_html, "msg_cdn_url")
    art.publish_text = _text(doc, "publish_time")
    art.publish_ts = js_num(raw_html, "ct")
    art.biz = js_var(raw_html, "biz") or _biz_from_url(url)
    art.mid = js_var(raw_html, "appmsgid") or js_var(raw_html, "mid")
    art.idx = js_var(raw_html, "idx") or "1"
    art.sn = js_var(raw_html, "sn")
    art.comment_id = js_var(raw_html, "comment_id")
    art.appmsg_token = js_var(raw_html, "appmsg_token")
    art.comment_enabled = _int_var(js_var(raw_html, "comment_enabled", "0"))

    # ---- 正文 ---- #
    body = doc.xpath('//*[@id="js_content"]')
    if body:
        node = body[0]
        art.content_html = lxml_html.tostring(node, encoding="unicode", method="html")
        art.content_html = art.content_html.replace('id="js_content"', 'id="js_content"', 1)
    else:
        # 新结构兜底：content_noencode 是纯文本，按段落还原
        plain = js_var(raw_html, "content_noencode")
        if plain:
            art.content_text = _html.unescape(plain)
            art.markdown = "\n\n".join(p.strip() for p in art.content_text.split("\n") if p.strip())

    if art.content_html:
        art.content_text = text_of(art.content_html)
        art.markdown = html_to_markdown(art.content_html, base=url)
        art.images = list(images_of(art.content_html, base=url))

    art.deleted = not art.title and not art.content_text
    return art


def _by_id(doc, eid: str):
    nodes = doc.xpath(f'//*[@id="{eid}"]')
    return nodes[0] if nodes else None


def _text(doc, eid: str) -> str:
    node = _by_id(doc, eid)
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def _meta(doc, prop: str) -> str:
    for expr in (f'//meta[@property="{prop}"]/@content', f'//meta[@name="{prop}"]/@content'):
        nodes = doc.xpath(expr)
        if nodes:
            return str(nodes[0]).strip()
    return ""


def _biz_from_url(url: str) -> str:
    q = parse_qs(urlparse(url).query)
    return (q.get("__biz") or [""])[0]


# --------------------------------------------------------------------------- #
def text_of(content_html: str) -> str:
    """按 DOM 顺序保留直接文本、子节点与尾文本。"""
    frag = lxml_html.fragment_fromstring(content_html, create_parent="div")

    def walk(el) -> str:
        tag = el.tag.lower() if isinstance(el.tag, str) else ""
        if tag in ("script", "style"):
            return ""
        if tag == "br":
            return "\n"
        parts = [el.text or ""]
        for child in el:
            parts.extend([walk(child), child.tail or ""])
        value = "".join(parts)
        return "\n" + value + "\n" if tag in BLOCK_TAGS or tag in ("ul", "ol", "table", "tr") else value

    lines = [re.sub(r"[ \t\u3000]+", " ", line).strip() for line in walk(frag).splitlines()]
    return "\n".join(line for line in lines if line)


def html_to_markdown(content_html: str, base: str = "") -> str:
    frag = lxml_html.fragment_fromstring(content_html, create_parent="div")

    def walk(el) -> str:
        tag = el.tag.lower() if isinstance(el.tag, str) else ""
        if tag in ("script", "style"):
            return ""
        if tag == "img":
            src = _img_src(el, base)
            return f"\n\n![]({src})\n\n" if src else ""
        if tag == "br":
            return "  \n"
        if tag == "pre":
            return "\n\n```\n" + "".join(el.itertext()).strip() + "\n```\n\n"
        value = el.text or ""
        for child in el:
            value += walk(child) + (child.tail or "")
        if tag in ("strong", "b"):
            return f"**{value}**"
        if tag in ("em", "i"):
            return f"*{value}*"
        if tag == "code":
            return f"`{value}`"
        if tag == "a" and el.get("href"):
            return f"[{value}]({_abs(el.get('href'), base)})"
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            value = "#" * int(tag[1]) + " " + value.strip()
        elif tag == "li":
            value = "- " + value.strip()
        elif tag == "blockquote":
            value = "\n".join("> " + line for line in value.strip().splitlines())
        if tag in BLOCK_TAGS or tag in ("ul", "ol", "table", "tr"):
            return "\n\n" + value.strip() + "\n\n"
        return value

    return re.sub(r"\n{3,}", "\n\n", walk(frag)).strip()


def _img_src(img, base: str) -> str:
    src = (img.get("data-src") or img.get("data-original") or img.get("src") or "").strip()
    if not src or src.startswith("data:"):
        return ""
    return _abs(src, base)


def images_of(content_html: str, base: str = "") -> Iterator[str]:
    frag = lxml_html.fragment_fromstring(content_html, create_parent="div")
    seen: set[str] = set()
    for img in frag.iter("img"):
        src = img.get("data-src") or img.get("data-original") or img.get("src") or ""
        src = src.strip()
        if not src or src.startswith("data:"):
            continue
        src = _abs(src, base)
        if src in seen:
            continue
        seen.add(src)
        yield src
    for source in frag.iter("mp-common-videosnap", "iframe"):
        src = source.get("data-vid") or source.get("data-src") or source.get("src") or ""
        if src.startswith("http") and src not in seen:
            seen.add(src)
            yield src


def _abs(src: str, base: str) -> str:
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("http"):
        return src
    return urljoin(base, src) if base else src
