"""下载流程编排：文章 -> 正文/图片文件 -> SQLite；评论 -> comments.json -> SQLite。"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .article import (Article, ArticleError, fetch_article_html, normalize_url, parse_article,
                      process_content, write_article_files)
from .comments import CommentError, comments_to_json, fetch_comments
from .config import Credentials, Settings
from .http import make_session, sleep_jitter
from .render_comments import attach_comments_to_files
from .storage import Store

_UNSAFE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_name(s: str, limit: int = 60) -> str:
    s = _UNSAFE.sub("_", s).strip(" ._")
    return (s[:limit] or "untitled").rstrip(" ._")


@dataclass
class DownloadResult:
    url: str
    ok: bool
    article: Article | None = None
    article_id: int | None = None
    dir_path: Path | None = None
    skipped: bool = False
    error: str = ""
    comment_count: int | None = None
    comment_error: str = ""


class Downloader:
    def __init__(self, settings: Settings, store: Store, cred: Credentials,
                 log: Callable[[str], None] = print):
        self.settings = settings
        self.store = store
        self.cred = cred
        self.log = log
        self.session = make_session(settings, cred)

    # ---- 目录 ------------------------------------------------------------
    def article_dir(self, art: Article) -> Path:
        account = safe_name(art.account_name or art.biz or "unknown-account", 40)
        return self.settings.articles_dir / account / f"{art.publish_date}_{safe_name(art.title)}_{art.key}"

    # ---- 文章 ------------------------------------------------------------
    def download(self, url: str, *, with_comments: bool = True, with_images: bool = True,
                 force: bool = False) -> DownloadResult:
        url = normalize_url(url)
        if not force:
            row = self.store.find_article(url)
            if row and row["status"] == "ok":
                res = DownloadResult(url=url, ok=True, article_id=row["id"], skipped=True,
                                     dir_path=Path(row["dir_path"]) if row["dir_path"] else None)
                if with_comments and row["comment_id"] and row["comments_fetched_at"] is None:
                    self._comments_for_row(row, res)
                return res

        try:
            html = fetch_article_html(self.session, url, timeout=self.settings.timeout,
                                      debug_dir=self.settings.data_dir / "debug")
            art = parse_article(html, url)
        except ArticleError as e:
            self.store.mark_failed(url, e.kind, str(e))
            return DownloadResult(url=url, ok=False, error=f"[{e.kind}] {e}")
        except Exception as e:  # 网络等其它异常
            self.store.mark_failed(url, "error", repr(e))
            return DownloadResult(url=url, ok=False, error=repr(e))

        out_dir = self.article_dir(art)
        process_content(art, html, out_dir, self.session, download_images=with_images,
                        delay=min(self.settings.delay / 4, 1.0), timeout=self.settings.timeout)
        write_article_files(art, out_dir, raw_html=html)
        article_id = self.store.upsert_article(art, out_dir)
        res = DownloadResult(url=url, ok=True, article=art, article_id=article_id, dir_path=out_dir)

        if with_comments:
            self.fetch_comments_for_article(art, article_id, out_dir, res)
        return res

    # ---- 评论 ------------------------------------------------------------
    def fetch_comments_for_article(self, art: Article, article_id: int, out_dir: Path,
                                   res: DownloadResult | None = None) -> DownloadResult:
        res = res or DownloadResult(url=art.canonical_url, ok=True, article=art, article_id=article_id, dir_path=out_dir)
        if not art.comment_id:
            self.store.replace_comments(article_id, [], error=None)
            res.comment_count = 0
            return res
        try:
            comments, last = fetch_comments(
                self.session, self.cred, biz=art.biz, mid=art.mid, idx=art.idx,
                comment_id=art.comment_id, appmsg_token=art.appmsg_token,
                delay=self.settings.delay, timeout=self.settings.timeout)
        except CommentError as e:
            self.store.set_comment_error(article_id, f"[{e.kind}] {e}")
            res.comment_error = f"[{e.kind}] {e}"
            return res
        except Exception as e:
            self.store.set_comment_error(article_id, repr(e))
            res.comment_error = repr(e)
            return res
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "comments.json").write_text(
            comments_to_json(comments, {"url": art.canonical_url, "title": art.title,
                                        "elected_total": last.get("elected_comment_total_cnt")}), "utf-8")
        self.store.replace_comments(article_id, comments)
        attach_comments_to_files(out_dir, comments)
        res.comment_count = len(comments)
        return res

    def _comments_for_row(self, row, res: DownloadResult) -> DownloadResult:
        art = Article(url=row["url"], biz=row["biz"] or "", mid=row["mid"] or "", idx=row["idx"] or "",
                      sn=row["sn"] or "", title=row["title"] or "", comment_id=row["comment_id"] or "")
        out_dir = Path(row["dir_path"]) if row["dir_path"] else self.article_dir(art)
        return self.fetch_comments_for_article(art, row["id"], out_dir, res)

    def refetch_comments(self, row) -> DownloadResult:
        return self._comments_for_row(row, DownloadResult(url=row["url"], ok=True, article_id=row["id"]))

    # ---- 批量 ------------------------------------------------------------
    def download_many(self, urls: list[str], *, with_comments: bool = True, with_images: bool = True,
                      force: bool = False, on_result: Callable[[int, int, DownloadResult], None] | None = None
                      ) -> list[DownloadResult]:
        results: list[DownloadResult] = []
        total = len(urls)
        fetched = 0  # 本轮真正发起过请求的篇数（跳过的不计）
        for i, url in enumerate(urls, 1):
            res = self.download(url, with_comments=with_comments, with_images=with_images, force=force)
            results.append(res)
            if on_result:
                on_result(i, total, res)
            if res.error and "[captcha]" in res.error:
                self.log("检测到微信验证码页面（环境异常），已停止后续下载，请等待 1~2 小时后再试，或调大 --delay")
                break
            if res.skipped or i >= total:
                continue
            fetched += 1
            if self.settings.rest_every and fetched % self.settings.rest_every == 0:
                self.log(f"已连续下载 {fetched} 篇，长休息约 {self.settings.rest_seconds:.0f} 秒……")
                sleep_jitter(self.settings.rest_seconds)
            else:
                sleep_jitter(self.settings.delay)
        return results


def fmt_ts(ts: int | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"
