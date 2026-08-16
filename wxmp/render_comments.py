"""把评论渲染进 article.html / article.md（追加“留言区”），便于离线阅读时直接看到评论。

渲染是幂等的：会先删除旧的留言区再追加新的，可反复调用。
"""
from __future__ import annotations

import html as htmlmod
import json
import re
import time
from pathlib import Path

from .comments import Comment

_HTML_BEGIN = "<!-- wxmp-comments-begin -->"
_HTML_END = "<!-- wxmp-comments-end -->"
_MD_BEGIN = "<!-- wxmp-comments-begin -->"
_MD_END = "<!-- wxmp-comments-end -->"

_CSS = (
    "<style>"
    "#wxmp-comments{margin-top:3em;padding-top:1.5em;border-top:1px solid #e5e5e5}"
    "#wxmp-comments h2{font-size:18px;color:#576b95}"
    ".wxc{display:flex;gap:10px;margin:14px 0}"
    ".wxc img{width:36px;height:36px;border-radius:50%;flex:none}"
    ".wxc .b{flex:1;min-width:0}"
    ".wxc .n{font-size:14px;color:#576b95}"
    ".wxc .t{font-size:12px;color:#999;margin-left:6px}"
    ".wxc .l{font-size:12px;color:#999;float:right}"
    ".wxc .c{white-space:pre-wrap;word-break:break-word;font-size:15px}"
    ".wxc.reply{margin:8px 0 8px 46px;padding:8px 10px;background:#f7f7f7;border-radius:6px}"
    ".wxc.reply.author .n{color:#07c160}"
    ".wxc.reply.author .n::after{content:'作者';font-size:11px;color:#fff;background:#07c160;"
    "border-radius:3px;padding:0 4px;margin-left:6px}"
    ".wxc .ne{color:#bbb;font-size:12px;margin-left:6px}"
    "</style>"
)


def _fmt(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


def _group(comments: list[Comment]) -> list[tuple[Comment, list[Comment]]]:
    top = [c for c in comments if not c.parent_content_id]
    replies: dict[str, list[Comment]] = {}
    for c in comments:
        if c.parent_content_id:
            replies.setdefault(c.parent_content_id, []).append(c)
    # 顶层按点赞数降序，再按时间；回复按时间
    top.sort(key=lambda c: (-c.like_num, c.create_time))
    for lst in replies.values():
        lst.sort(key=lambda c: c.create_time)
    return [(c, replies.get(c.content_id, [])) for c in top]


def render_comments_html(comments: list[Comment]) -> str:
    esc = htmlmod.escape
    grouped = _group(comments)
    n_top = len(grouped)
    parts = [_HTML_BEGIN, _CSS, '<section id="wxmp-comments">',
             f"<h2>留言 {n_top}{'' if n_top == len(comments) else f'（含回复共 {len(comments)}）'}</h2>"]
    if not comments:
        parts.append('<p style="color:#999">暂无留言</p>')

    def one(c: Comment, cls: str) -> str:
        avatar = f'<img src="{esc(c.logo_url)}" alt="" loading="lazy">' if c.logo_url and cls == "wxc" else ""
        like = f'<span class="l">赞 {c.like_num}</span>' if c.like_num else ""
        ne = '<span class="ne">非精选</span>' if not c.is_elected else ""
        return (f'<div class="{cls}">{avatar}<div class="b">{like}'
                f'<span class="n">{esc(c.nick_name)}</span><span class="t">{_fmt(c.create_time)}</span>{ne}'
                f'<div class="c">{esc(c.content)}</div></div></div>')

    for c, reps in grouped:
        parts.append(one(c, "wxc"))
        for r in reps:
            parts.append(one(r, "wxc reply" + (" author" if r.is_author else "")))
    parts += ["</section>", _HTML_END]
    return "\n".join(parts)


def render_comments_md(comments: list[Comment]) -> str:
    grouped = _group(comments)
    lines = [_MD_BEGIN, "", "---", "", f"## 留言（{len(grouped)} 条，含回复共 {len(comments)} 条）", ""]
    if not comments:
        lines.append("_暂无留言_")
    for c, reps in grouped:
        like = f" 👍{c.like_num}" if c.like_num else ""
        ne = "" if c.is_elected else " _(非精选)_"
        body = c.content.replace("\n", "\n  ")
        lines.append(f"- **{c.nick_name}**{like} · {_fmt(c.create_time)}{ne}\n  {body}")
        for r in reps:
            who = "作者" if r.is_author else r.nick_name
            rb = r.content.replace("\n", "\n    ")
            lines.append(f"  - ↳ **{who}** · {_fmt(r.create_time)}\n    {rb}")
    lines += ["", _MD_END, ""]
    return "\n".join(lines)


def _strip(text: str, begin: str, end: str) -> str:
    return re.sub(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", "", text, flags=re.S)


def attach_comments_to_files(out_dir: Path, comments: list[Comment]) -> None:
    """把留言区写入 out_dir 下的 article.html / article.md（若存在）。幂等。"""
    html_path = out_dir / "article.html"
    if html_path.exists():
        html = _strip(html_path.read_text("utf-8"), _HTML_BEGIN, _HTML_END)
        block = render_comments_html(comments)
        if "</body>" in html:
            html = html.replace("</body>", block + "\n</body>", 1)
        else:
            html += "\n" + block
        html_path.write_text(html, "utf-8")
    md_path = out_dir / "article.md"
    if md_path.exists():
        md = _strip(md_path.read_text("utf-8"), _MD_BEGIN, _MD_END).rstrip("\n") + "\n\n"
        md_path.write_text(md + render_comments_md(comments), "utf-8")


def load_comments_json(path: Path) -> list[Comment]:
    d = json.loads(path.read_text("utf-8"))
    out = []
    for x in d.get("comments", []):
        out.append(Comment(content_id=str(x.get("content_id", "")), parent_content_id=str(x.get("parent_content_id", "")),
                           nick_name=x.get("nick_name", ""), logo_url=x.get("logo_url", ""),
                           content=x.get("content", ""), create_time=int(x.get("create_time") or 0),
                           like_num=int(x.get("like_num") or 0), is_elected=bool(x.get("is_elected", True)),
                           is_author=bool(x.get("is_author", False)), raw=x.get("raw") or {}))
    return out
