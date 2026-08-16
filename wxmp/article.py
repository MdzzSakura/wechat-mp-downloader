"""文章页抓取与解析：元数据、正文 HTML、图片本地化、Markdown 转换。"""
from __future__ import annotations

import hashlib
import html as htmlmod
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify

from .http import request_with_retry, sleep_jitter


class ArticleError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


# 页面文案 -> 错误类型
ERROR_MARKERS = [
    ("该内容已被发布者删除", "deleted"),
    ("此内容因违规无法查看", "blocked"),
    ("此内容发送失败无法查看", "blocked"),
    ("此帐号已被屏蔽", "blocked"),
    ("该公众号已迁移", "migrated"),
    ("环境异常", "captcha"),
    ("操作频繁", "rate_limited"),
    ("链接已过期", "expired"),
    ("参数错误", "bad_params"),
]


@dataclass
class Article:
    url: str
    biz: str = ""
    mid: str = ""
    idx: str = ""
    sn: str = ""
    title: str = ""
    author: str = ""
    account_name: str = ""
    gh_id: str = ""
    publish_time: int | None = None
    digest: str = ""
    cover_url: str = ""
    comment_id: str = ""
    appmsg_token: str = ""
    content_html: str = ""                       # 处理后正文（图片已指向本地相对路径）
    content_md: str = ""
    images: list[dict] = field(default_factory=list)  # [{"url": ..., "path": "images/001.jpg" | None}]

    @property
    def canonical_url(self) -> str:
        if self.biz and self.mid and self.idx and self.sn:
            return f"https://mp.weixin.qq.com/s?__biz={self.biz}&mid={self.mid}&idx={self.idx}&sn={self.sn}"
        return self.url

    @property
    def key(self) -> str:
        if self.mid and self.idx:
            return f"{self.mid}_{self.idx}"
        return hashlib.sha1(self.url.encode()).hexdigest()[:10]

    @property
    def publish_date(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self.publish_time)) if self.publish_time else "unknown-date"

    @property
    def publish_time_str(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.publish_time)) if self.publish_time else ""

    def meta_dict(self) -> dict:
        d = asdict(self)
        d.pop("content_html")
        d.pop("content_md")
        d["canonical_url"] = self.canonical_url
        d["publish_time_str"] = self.publish_time_str
        return d


# ---------------------------------------------------------------------------
# URL / 页面辅助
# ---------------------------------------------------------------------------
def normalize_url(url: str) -> str:
    url = htmlmod.unescape(url.strip())
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://mp.weixin.qq.com" + url
    return url.replace("http://mp.weixin.qq.com", "https://mp.weixin.qq.com")


def parse_url_params(url: str) -> dict[str, str]:
    q = parse_qs(urlparse(url).query)

    def pick(k: str) -> str:
        return (q.get(k) or [""])[0]

    return {"biz": pick("__biz"), "mid": pick("mid"), "idx": pick("idx"), "sn": pick("sn")}


_JS_STR = re.compile(r"""(['"])(.*?)(?<!\\)\1""")


def js_var(html: str, name: str) -> str:
    """提取页面脚本里 `var name = "" || "value";` / `window.name = 'value'.html(false);` 形式的字符串值。"""
    m = re.search(rf"""(?:\bvar\s+|window\.)\s*{re.escape(name)}\s*=\s*([^;\n]*)""", html)
    if not m:
        return ""
    for _, v in _JS_STR.findall(m.group(1)):
        v = v.strip()
        if v:
            if "\\x" in v or "\\u" in v:
                v = v.encode("utf-8").decode("unicode_escape", "ignore")
            return htmlmod.unescape(v)
    return ""


def _page_hint(html: str, final_url: str = "") -> str:
    """失败时给出页面标题 / 可见文字片段，便于判断是验证码、跳转还是结构变化。"""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    title = htmlmod.unescape(m.group(1)).strip() if m else ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", htmlmod.unescape(text)).strip()
    parts = []
    if final_url:
        parts.append(f"url={final_url}")
    if title:
        parts.append(f"title={title[:60]}")
    if text:
        parts.append(f"text={text[:120]}")
    return "；".join(parts)


def detect_error(html: str, final_url: str = "") -> None:
    if 'id="js_content"' in html:
        return
    for marker, kind in ERROR_MARKERS:
        if marker in html:
            raise ArticleError(kind, f"页面提示：{marker}")
    hint = _page_hint(html, final_url)
    raise ArticleError("unknown", "未找到正文节点 #js_content（页面结构变化或非文章页）"
                       + (f"。响应摘要：{hint}" if hint else ""))


def fetch_article_html(session: requests.Session, url: str, timeout: float = 20.0,
                       debug_dir: Path | None = None) -> str:
    resp = request_with_retry(session, "GET", url, timeout=timeout)
    resp.encoding = "utf-8"
    html = resp.text
    try:
        detect_error(html, resp.url)
    except ArticleError:
        if debug_dir is not None:
            debug_dir.mkdir(parents=True, exist_ok=True)
            name = re.sub(r"[^A-Za-z0-9_-]+", "_", url.rsplit("/", 1)[-1])[:60] or "page"
            (debug_dir / f"{int(time.time())}_{name}.html").write_text(html, "utf-8")
        raise
    return html


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def _meta(soup: BeautifulSoup, **attrs) -> str:
    tag = soup.find("meta", attrs=attrs)
    return htmlmod.unescape(tag.get("content", "").strip()) if tag else ""


def _text(soup: BeautifulSoup, selector: str) -> str:
    node = soup.select_one(selector)
    return node.get_text(" ", strip=True) if node else ""


def parse_article(html: str, url: str) -> Article:
    soup = BeautifulSoup(html, "lxml")
    art = Article(url=normalize_url(url))

    p = parse_url_params(art.url)
    art.biz = p["biz"] or js_var(html, "biz")
    art.mid = p["mid"] or js_var(html, "mid")
    art.idx = p["idx"] or js_var(html, "idx")
    art.sn = p["sn"] or js_var(html, "sn")

    art.title = (
        _meta(soup, property="og:title")
        or js_var(html, "msg_title")
        or _text(soup, "#activity-name")
        or (soup.title.get_text(strip=True) if soup.title else "")
    )
    art.digest = _meta(soup, property="og:description") or js_var(html, "msg_desc")
    art.cover_url = _meta(soup, property="og:image") or js_var(html, "msg_cdn_url")
    art.author = _meta(soup, name="author") or js_var(html, "author") or _text(soup, "#js_author_name")
    art.account_name = _text(soup, "#js_name") or _text(soup, ".profile_nickname") or js_var(html, "nickname")
    art.gh_id = js_var(html, "user_name")
    art.comment_id = js_var(html, "comment_id")
    if art.comment_id == "0":
        art.comment_id = ""
    art.appmsg_token = js_var(html, "appmsg_token")

    ct = js_var(html, "ct")
    if ct.isdigit():
        art.publish_time = int(ct)
    else:
        m = re.search(r"createTime\s*=\s*['\"](\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?)", html)
        if m:
            s = m.group(1)
            fmt = "%Y-%m-%d %H:%M:%S" if s.count(":") == 2 else ("%Y-%m-%d %H:%M" if ":" in s else "%Y-%m-%d")
            art.publish_time = int(time.mktime(time.strptime(s, fmt)))
    return art


# ---------------------------------------------------------------------------
# 正文处理：图片本地化 + Markdown
# ---------------------------------------------------------------------------
_EXT_MAP = {"jpeg": "jpg", "jpg": "jpg", "png": "png", "gif": "gif", "webp": "webp", "bmp": "bmp", "svg": "svg"}


def _guess_ext(src: str, data_type: str | None, content_type: str | None) -> str:
    if data_type and data_type.lower() in _EXT_MAP:
        return _EXT_MAP[data_type.lower()]
    m = re.search(r"wx_fmt=(\w+)", src)
    if m and m.group(1).lower() in _EXT_MAP:
        return _EXT_MAP[m.group(1).lower()]
    if content_type:
        sub = content_type.split("/")[-1].split(";")[0].lower()
        if sub in _EXT_MAP:
            return _EXT_MAP[sub]
    return "jpg"


def download_image(session: requests.Session, src: str, images_dir: Path, index: int,
                   data_type: str | None = None, timeout: float = 20.0) -> Path | None:
    images_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{index:03d}"
    existing = [p for p in images_dir.glob(stem + ".*") if p.stat().st_size > 0]
    if existing:
        return existing[0]
    try:
        resp = request_with_retry(session, "GET", src, timeout=timeout,
                                  headers={"Referer": "https://mp.weixin.qq.com/"})
        if resp.status_code != 200 or not resp.content:
            return None
    except requests.RequestException:
        return None
    ext = _guess_ext(src, data_type, resp.headers.get("Content-Type"))
    path = images_dir / f"{stem}.{ext}"
    path.write_bytes(resp.content)
    return path


_IMG_NOISE_ATTRS = ("data-src", "data-s", "data-ratio", "data-w", "data-type", "data-fail",
                    "data-croporisrc", "data-backw", "data-backh", "crossorigin", "class", "style")


def process_content(art: Article, html: str, out_dir: Path, session: requests.Session | None,
                    download_images: bool = True, delay: float = 0.3, timeout: float = 20.0) -> None:
    """填充 art.content_html / content_md / images，图片下载到 out_dir/images。"""
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one("#js_content")
    if node is None:
        raise ArticleError("unknown", "未找到正文节点 #js_content")

    style = node.get("style", "")
    style = re.sub(r"(visibility\s*:\s*hidden|opacity\s*:\s*0)\s*;?", "", style).strip()
    if style:
        node["style"] = style
    elif "style" in node.attrs:
        del node["style"]

    for t in node.find_all(["script", "style"]):
        t.decompose()

    for iframe in node.find_all("iframe"):
        src = iframe.get("data-src") or iframe.get("src") or ""
        p = soup.new_tag("p")
        a = soup.new_tag("a", href=src)
        a.string = f"[视频] {src}"
        p.append(a)
        iframe.replace_with(p)

    images_dir = out_dir / "images"
    art.images = []
    n = 0
    for img in node.find_all("img"):
        src = img.get("data-src") or img.get("src") or ""
        src = normalize_url(src) if src else ""
        if not src or src.startswith("data:"):
            continue
        n += 1
        local: Path | None = None
        if download_images and session is not None:
            local = download_image(session, src, images_dir, n, img.get("data-type"), timeout=timeout)
            sleep_jitter(delay)
        rel = f"images/{local.name}" if local else None
        img["src"] = rel or src
        img["data-original-src"] = src
        for attr in _IMG_NOISE_ATTRS:
            if attr in img.attrs:
                del img[attr]
        if not img.get("alt"):
            img["alt"] = f"image-{n:03d}"
        art.images.append({"url": src, "path": rel})

    art.content_html = node.decode_contents().strip()
    md = markdownify(art.content_html, heading_style="ATX", bullets="-")
    art.content_md = re.sub(r"\n{3,}", "\n\n", md).strip()


# ---------------------------------------------------------------------------
# 输出文件
# ---------------------------------------------------------------------------
def render_markdown(art: Article) -> str:
    fm = {
        "title": art.title,
        "account": art.account_name,
        "author": art.author,
        "publish_time": art.publish_time_str,
        "url": art.canonical_url,
        "digest": art.digest,
    }
    lines = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items()] + ["---", ""]
    lines += [f"# {art.title}", "", art.content_md, ""]
    return "\n".join(lines)


def render_html(art: Article) -> str:
    esc = htmlmod.escape
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        f"<title>{esc(art.title)}</title>"
        "<style>body{max-width:720px;margin:2em auto;padding:0 1em;"
        'font:16px/1.8 -apple-system,"Microsoft YaHei",sans-serif;color:#333}'
        "img{max-width:100%;height:auto}.meta{color:#888;font-size:14px}</style></head><body>"
        f"<h1>{esc(art.title)}</h1>"
        f'<p class="meta">{esc(art.account_name)} · {esc(art.author)} · {esc(art.publish_time_str)} · '
        f'<a href="{esc(art.canonical_url)}">原文</a></p>'
        f'<div id="js_content">{art.content_html}</div></body></html>'
    )


def write_article_files(art: Article, out_dir: Path, raw_html: str | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "article.md").write_text(render_markdown(art), "utf-8")
    (out_dir / "article.html").write_text(render_html(art), "utf-8")
    (out_dir / "meta.json").write_text(json.dumps(art.meta_dict(), ensure_ascii=False, indent=2), "utf-8")
    if raw_html is not None:
        (out_dir / "raw.html").write_text(raw_html, "utf-8")
