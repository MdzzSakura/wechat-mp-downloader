"""评论（留言）抓取：/mp/appmsg_comment?action=getcomment。

该接口必须携带微信客户端凭证（uin / key / pass_ticket / appmsg_token 以及 wap_sid2 等 Cookie），
凭证由 `wxmp capture` 抓取。若抓到了完整的 getcomment 请求模板，则直接回放模板并替换
__biz / appmsgid / idx / comment_id / offset；否则用散参数拼一条默认请求。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

import requests

from .config import Credentials
from .http import request_with_retry, sleep_jitter

COMMENT_URL = "https://mp.weixin.qq.com/mp/appmsg_comment"

_DEFAULT_QUERY = {
    "action": "getcomment",
    "scene": "0",
    "offset": "0",
    "limit": "100",
    "send_time": "",
    "sessionid": "",
    "enterid": "",
    "is_need_comment": "1",
    "is_need_reward": "1",
    "wxtoken": "777",
    "x5": "0",
    "f": "json",
}


class CommentError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class Comment:
    content_id: str
    parent_content_id: str          # 顶层留言为 ""，作者/他人回复为所属留言的 content_id
    nick_name: str
    logo_url: str
    content: str
    create_time: int
    like_num: int
    is_elected: bool                # 是否精选留言（is_elected=False 表示“我的留言”/朋友留言）
    is_author: bool                 # 是否作者回复
    raw: dict

    def to_dict(self) -> dict:
        return asdict(self)


def build_request(cred: Credentials, biz: str, mid: str, idx: str, comment_id: str,
                  offset: int, limit: int, appmsg_token: str = "") -> tuple[str, str, dict, dict, str | None]:
    tpl = cred.templates.get("getcomment")
    if tpl:
        method, url, params, headers, body = tpl.method, tpl.url, dict(tpl.query), dict(tpl.headers), tpl.body
    else:
        method, url, params, headers, body = "GET", COMMENT_URL, dict(_DEFAULT_QUERY), {}, None
        params.update({
            "uin": cred.uin, "key": cred.key, "pass_ticket": cred.pass_ticket,
            "appmsg_token": cred.appmsg_token,
            "devicetype": cred.devicetype or "Windows 10 x64",
            "clientversion": cred.clientversion or "63090c11",
        })
    params.update({
        "__biz": biz, "appmsgid": mid, "idx": idx, "comment_id": comment_id,
        "offset": str(offset), "limit": str(limit),
    })
    # 更新的散参数优先（模板可能比 credentials 里的散参数旧）
    for k in ("uin", "key", "pass_ticket"):
        v = getattr(cred, k)
        if v:
            params[k] = v
    token = cred.appmsg_token or appmsg_token
    if token:
        params["appmsg_token"] = token
    if cred.cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cred.cookies.items())
    if cred.user_agent:
        headers["User-Agent"] = cred.user_agent
    headers.setdefault("Referer", f"https://mp.weixin.qq.com/s?__biz={biz}&mid={mid}&idx={idx}")
    return method, url, params, headers, body


def _to_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def normalize_comments(data: dict) -> list[Comment]:
    """把接口返回的 JSON 拉平成 Comment 列表（含回复）。"""
    out: list[Comment] = []
    groups = (("elected_comment", True), ("my_comment", False), ("friend_comment", False))
    for key, elected in groups:
        for c in data.get(key) or []:
            cid = str(c.get("content_id") or c.get("id") or "")
            out.append(Comment(
                content_id=cid, parent_content_id="",
                nick_name=c.get("nick_name", ""), logo_url=c.get("logo_url", ""),
                content=c.get("content", ""), create_time=_to_int(c.get("create_time")),
                like_num=_to_int(c.get("like_num")), is_elected=elected,
                is_author=False, raw=c,
            ))
            replies = (c.get("reply_new") or c.get("reply") or {}).get("reply_list") or []
            for i, r in enumerate(replies):
                out.append(Comment(
                    content_id=str(r.get("reply_id") or r.get("content_id") or f"{cid}-r{i}"),
                    parent_content_id=cid,
                    nick_name=r.get("nick_name", ""), logo_url=r.get("logo_url", ""),
                    content=r.get("content", ""), create_time=_to_int(r.get("create_time")),
                    like_num=_to_int(r.get("reply_like_num") or r.get("like_num")),
                    is_elected=elected,
                    is_author=bool(_to_int(r.get("is_from"))), raw=r,
                ))
    return out


def fetch_comments(session: requests.Session, cred: Credentials, *, biz: str, mid: str, idx: str,
                   comment_id: str, appmsg_token: str = "", limit: int = 100, delay: float = 1.0,
                   timeout: float = 20.0, max_pages: int = 50) -> tuple[list[Comment], dict]:
    """分页抓取全部留言。返回 (comments, 最后一页原始响应)。"""
    if not comment_id:
        return [], {}
    if not cred.has_comment_access():
        raise CommentError("no_credentials", "缺少评论接口凭证，请先运行 `wxmp capture` 并在微信中打开一篇文章的评论区")

    all_comments: list[Comment] = []
    seen: set[tuple[str, str]] = set()
    offset = 0
    last: dict = {}
    for _ in range(max_pages):
        method, url, params, headers, body = build_request(
            cred, biz, mid, idx, comment_id, offset, limit, appmsg_token)
        resp = request_with_retry(session, method, url, params=params, headers=headers,
                                  data=body, timeout=timeout)
        try:
            data = resp.json()
        except json.JSONDecodeError:
            snippet = resp.text[:200].replace("\n", " ")
            raise CommentError("bad_response", f"评论接口未返回 JSON（凭证可能失效）：{snippet}")
        ret = (data.get("base_resp") or {}).get("ret", data.get("ret", 0))
        if ret != 0:
            msg = (data.get("base_resp") or {}).get("errmsg") or data.get("errmsg") or ""
            kind = "expired" if ret in (-1, -6, -3, 1) else "api_error"
            raise CommentError(kind, f"评论接口返回 ret={ret} {msg}（appmsg_token/key 可能已过期，请重新 capture）")
        last = data
        page = normalize_comments(data)
        fresh = [c for c in page if (c.content_id, c.parent_content_id) not in seen]
        for c in fresh:
            seen.add((c.content_id, c.parent_content_id))
        all_comments.extend(fresh)

        top_cnt = len(data.get("elected_comment") or [])
        total = _to_int(data.get("elected_comment_total_cnt"), -1)
        offset += top_cnt
        if top_cnt < limit or (total >= 0 and offset >= total) or not fresh:
            break
        sleep_jitter(delay)
    return all_comments, last


def comments_to_json(comments: list[Comment], meta: dict | None = None) -> str:
    return json.dumps({
        "fetched_at": int(time.time()),
        "count": len(comments),
        "meta": meta or {},
        "comments": [c.to_dict() for c in comments],
    }, ensure_ascii=False, indent=2)
