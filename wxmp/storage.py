"""SQLite 结构化存储。

表：
- accounts   公众号（biz 为主键）
- articles   文章元数据 + 正文（HTML / Markdown）+ 本地目录
- comments   留言与回复（parent_content_id 为空表示顶层留言）
- images     文章图片 URL 与本地相对路径
- history    从历史列表接口抓到的文章清单（用于增量下载）
"""
from __future__ import annotations

import csv
import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from .article import Article
from .comments import Comment
from .history import HistoryItem

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS accounts (
    biz         TEXT PRIMARY KEY,
    name        TEXT,
    gh_id       TEXT,
    updated_at  INTEGER
);

CREATE TABLE IF NOT EXISTS articles (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    url                 TEXT NOT NULL UNIQUE,
    biz                 TEXT DEFAULT '',
    mid                 TEXT DEFAULT '',
    idx                 TEXT DEFAULT '',
    sn                  TEXT DEFAULT '',
    title               TEXT,
    author              TEXT,
    account_name        TEXT,
    publish_time        INTEGER,
    digest              TEXT,
    cover_url           TEXT,
    comment_id          TEXT,
    content_html        TEXT,
    content_md          TEXT,
    dir_path            TEXT,
    status              TEXT DEFAULT 'ok',      -- ok / deleted / blocked / captcha / error ...
    error               TEXT,
    fetched_at          INTEGER,
    comments_fetched_at INTEGER,
    comment_count       INTEGER DEFAULT 0,
    comment_error       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_articles_biz_mid_idx ON articles(biz, mid, idx) WHERE mid <> '';
CREATE INDEX IF NOT EXISTS ix_articles_publish ON articles(publish_time);

CREATE TABLE IF NOT EXISTS comments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id        INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    content_id        TEXT NOT NULL,
    parent_content_id TEXT NOT NULL DEFAULT '',
    nick_name         TEXT,
    logo_url          TEXT,
    content           TEXT,
    create_time       INTEGER,
    like_num          INTEGER,
    is_elected        INTEGER,
    is_author         INTEGER,
    raw               TEXT,
    UNIQUE(article_id, content_id, parent_content_id)
);

CREATE TABLE IF NOT EXISTS images (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id  INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    path        TEXT,
    UNIQUE(article_id, url)
);

CREATE TABLE IF NOT EXISTS history (
    biz          TEXT NOT NULL,
    mid          TEXT NOT NULL,
    idx          TEXT NOT NULL,
    title        TEXT,
    url          TEXT,
    publish_time INTEGER,
    digest       TEXT,
    cover_url    TEXT,
    author       TEXT,
    source       TEXT,
    seen_at      INTEGER,
    PRIMARY KEY (biz, mid, idx)
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---- accounts ------------------------------------------------------
    def upsert_account(self, biz: str, name: str = "", gh_id: str = "") -> None:
        if not biz:
            return
        self.conn.execute(
            """INSERT INTO accounts(biz, name, gh_id, updated_at) VALUES(?,?,?,?)
               ON CONFLICT(biz) DO UPDATE SET
                 name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE accounts.name END,
                 gh_id=CASE WHEN excluded.gh_id<>'' THEN excluded.gh_id ELSE accounts.gh_id END,
                 updated_at=excluded.updated_at""",
            (biz, name, gh_id, int(time.time())))
        self.conn.commit()

    # ---- articles ------------------------------------------------------
    def find_article(self, url: str = "", biz: str = "", mid: str = "", idx: str = "") -> sqlite3.Row | None:
        if biz and mid and idx:
            row = self.conn.execute("SELECT * FROM articles WHERE biz=? AND mid=? AND idx=?",
                                    (biz, mid, idx)).fetchone()
            if row:
                return row
        if url:
            return self.conn.execute("SELECT * FROM articles WHERE url=?", (url,)).fetchone()
        return None

    def get_article(self, article_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()

    def upsert_article(self, art: Article, dir_path: Path | str) -> int:
        now = int(time.time())
        row = self.find_article(art.canonical_url, art.biz, art.mid, art.idx) or self.find_article(art.url)
        fields = dict(
            url=art.canonical_url, biz=art.biz, mid=art.mid, idx=art.idx, sn=art.sn, title=art.title,
            author=art.author, account_name=art.account_name, publish_time=art.publish_time,
            digest=art.digest, cover_url=art.cover_url, comment_id=art.comment_id,
            content_html=art.content_html, content_md=art.content_md, dir_path=str(dir_path),
            status="ok", error=None, fetched_at=now,
        )
        if row:
            sets = ", ".join(f"{k}=?" for k in fields)
            self.conn.execute(f"UPDATE articles SET {sets} WHERE id=?", (*fields.values(), row["id"]))
            article_id = row["id"]
        else:
            cols = ", ".join(fields)
            qs = ", ".join("?" for _ in fields)
            cur = self.conn.execute(f"INSERT INTO articles({cols}) VALUES({qs})", tuple(fields.values()))
            article_id = cur.lastrowid
        self.conn.execute("DELETE FROM images WHERE article_id=?", (article_id,))
        self.conn.executemany("INSERT OR IGNORE INTO images(article_id, url, path) VALUES(?,?,?)",
                              [(article_id, im["url"], im.get("path")) for im in art.images])
        self.conn.commit()
        self.upsert_account(art.biz, art.account_name, art.gh_id)
        return int(article_id)

    def mark_failed(self, url: str, kind: str, message: str) -> None:
        now = int(time.time())
        row = self.find_article(url)
        if row:
            self.conn.execute("UPDATE articles SET status=?, error=?, fetched_at=? WHERE id=?",
                              (kind, message, now, row["id"]))
        else:
            self.conn.execute("INSERT INTO articles(url, status, error, fetched_at) VALUES(?,?,?,?)",
                              (url, kind, message, now))
        self.conn.commit()

    def list_articles(self, biz: str = "", status: str = "", limit: int = 100, offset: int = 0) -> list[sqlite3.Row]:
        sql = "SELECT * FROM articles WHERE 1=1"
        args: list = []
        if biz:
            sql += " AND biz=?"
            args.append(biz)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY publish_time DESC, id DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        return self.conn.execute(sql, args).fetchall()

    def articles_pending_comments(self, biz: str = "") -> list[sqlite3.Row]:
        sql = ("SELECT * FROM articles WHERE status='ok' AND comment_id<>'' AND comment_id IS NOT NULL "
               "AND comments_fetched_at IS NULL")
        args: list = []
        if biz:
            sql += " AND biz=?"
            args.append(biz)
        return self.conn.execute(sql + " ORDER BY publish_time DESC", args).fetchall()

    # ---- comments ------------------------------------------------------
    def replace_comments(self, article_id: int, comments: Iterable[Comment], error: str | None = None) -> int:
        comments = list(comments)
        self.conn.execute("DELETE FROM comments WHERE article_id=?", (article_id,))
        self.conn.executemany(
            """INSERT OR IGNORE INTO comments(article_id, content_id, parent_content_id, nick_name, logo_url,
                 content, create_time, like_num, is_elected, is_author, raw)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            [(article_id, c.content_id, c.parent_content_id, c.nick_name, c.logo_url, c.content,
              c.create_time, c.like_num, int(c.is_elected), int(c.is_author),
              json.dumps(c.raw, ensure_ascii=False)) for c in comments])
        self.conn.execute(
            "UPDATE articles SET comments_fetched_at=?, comment_count=?, comment_error=? WHERE id=?",
            (int(time.time()), len(comments), error, article_id))
        self.conn.commit()
        return len(comments)

    def set_comment_error(self, article_id: int, error: str) -> None:
        self.conn.execute("UPDATE articles SET comment_error=? WHERE id=?", (error, article_id))
        self.conn.commit()

    def get_comments(self, article_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM comments WHERE article_id=? ORDER BY parent_content_id, create_time", (article_id,)
        ).fetchall()

    # ---- history -------------------------------------------------------
    def add_history(self, items: Iterable[HistoryItem]) -> int:
        now = int(time.time())
        rows = [(i.biz, i.mid, i.idx, i.title, i.url, i.publish_time, i.digest, i.cover_url, i.author, i.source, now)
                for i in items]
        cur = self.conn.executemany(
            """INSERT INTO history(biz, mid, idx, title, url, publish_time, digest, cover_url, author, source, seen_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(biz, mid, idx) DO UPDATE SET title=excluded.title, url=excluded.url,
                 publish_time=excluded.publish_time, seen_at=excluded.seen_at""",
            rows)
        self.conn.commit()
        return cur.rowcount if cur.rowcount is not None else len(rows)

    def history_not_downloaded(self, biz: str, since: int | None = None) -> list[sqlite3.Row]:
        sql = """SELECT h.* FROM history h
                 LEFT JOIN articles a ON a.biz=h.biz AND a.mid=h.mid AND a.idx=h.idx AND a.status='ok'
                 WHERE h.biz=? AND a.id IS NULL"""
        args: list = [biz]
        if since:
            sql += " AND h.publish_time>=?"
            args.append(since)
        return self.conn.execute(sql + " ORDER BY h.publish_time DESC", args).fetchall()

    def history_count(self, biz: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM history WHERE biz=?", (biz,)).fetchone()[0]

    # ---- export / stats -----------------------------------------------
    def stats(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "accounts": q("SELECT COUNT(*) FROM accounts"),
            "articles_ok": q("SELECT COUNT(*) FROM articles WHERE status='ok'"),
            "articles_failed": q("SELECT COUNT(*) FROM articles WHERE status<>'ok'"),
            "comments": q("SELECT COUNT(*) FROM comments"),
            "images": q("SELECT COUNT(*) FROM images"),
            "history": q("SELECT COUNT(*) FROM history"),
        }

    def export(self, out: Path, fmt: str = "json", biz: str = "", with_content: bool = False) -> int:
        rows = self.list_articles(biz=biz, status="ok", limit=10**9)
        out.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "json":
            data = []
            for r in rows:
                d = dict(r)
                if not with_content:
                    d.pop("content_html", None)
                    d.pop("content_md", None)
                d["comments"] = [
                    {k: c[k] for k in ("content_id", "parent_content_id", "nick_name", "content",
                                       "create_time", "like_num", "is_elected", "is_author")}
                    for c in self.get_comments(r["id"])
                ]
                data.append(d)
            out.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        elif fmt == "csv":
            cols = ["id", "biz", "account_name", "title", "author", "publish_time", "url", "comment_count", "dir_path"]
            with out.open("w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(cols)
                for r in rows:
                    w.writerow([r[c] for c in cols])
        else:
            raise ValueError(f"不支持的导出格式: {fmt}")
        return len(rows)
