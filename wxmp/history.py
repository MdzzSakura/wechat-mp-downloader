"""公众号历史文章列表的两种来源：

1. 微信客户端历史消息接口 /mp/profile_ext?action=getmsg（凭证由 `wxmp capture` 抓取，
   需要在微信里打开该公众号的“历史消息 / 消息列表”页面一次；key 一般与 __biz 绑定）。
2. 公众平台后台 mp.weixin.qq.com（需自己有一个公众号并登录，把 token 与 Cookie 写入 credentials）。
"""
from __future__ import annotations

import html as htmlmod
import json
import random
import time
from dataclasses import asdict, dataclass
from typing import Iterator

import requests

from .article import normalize_url, parse_url_params
from .config import Credentials
from .http import request_with_retry, sleep_jitter

PROFILE_URL = "https://mp.weixin.qq.com/mp/profile_ext"
MP_SEARCH_URL = "https://mp.weixin.qq.com/cgi-bin/searchbiz"
MP_LIST_URL = "https://mp.weixin.qq.com/cgi-bin/appmsg"


class HistoryError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class HistoryItem:
    biz: str
    mid: str
    idx: str
    title: str
    url: str
    publish_time: int
    digest: str = ""
    cover_url: str = ""
    author: str = ""
    source: str = "wechat"    # wechat / mp

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 1. 微信客户端 profile_ext getmsg
# ---------------------------------------------------------------------------
def _build_getmsg(cred: Credentials, biz: str, offset: int, count: int) -> tuple[str, str, dict, dict]:
    tpl = cred.templates.get(f"getmsg:{biz}") or cred.templates.get("getmsg")
    if tpl:
        method, url, params, headers = tpl.method, tpl.url, dict(tpl.query), dict(tpl.headers)
    else:
        method, url, headers = "GET", PROFILE_URL, {}
        params = {
            "action": "getmsg", "f": "json", "is_ok": "1", "scene": "124", "x5": "0",
            "uin": cred.uin, "key": cred.key, "pass_ticket": cred.pass_ticket,
            "wxtoken": "", "appmsg_token": cred.appmsg_token,
        }
    params.update({"__biz": biz, "offset": str(offset), "count": str(count)})
    if cred.cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cred.cookies.items())
    if cred.user_agent:
        headers["User-Agent"] = cred.user_agent
    headers.setdefault("Referer", f"https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz={biz}&scene=124")
    return method, url, params, headers


def _item_from_ext(biz: str, info: dict, ext: dict) -> HistoryItem | None:
    url = ext.get("content_url") or ""
    if not url:
        return None
    url = normalize_url(htmlmod.unescape(url))
    p = parse_url_params(url)
    return HistoryItem(
        biz=p["biz"] or biz, mid=p["mid"] or str(info.get("id", "")), idx=p["idx"] or "1",
        title=htmlmod.unescape(ext.get("title", "")), url=url,
        publish_time=int(info.get("datetime") or 0),
        digest=htmlmod.unescape(ext.get("digest", "")), cover_url=ext.get("cover", ""),
        author=ext.get("author", ""), source="wechat",
    )


def iter_history_wechat(session: requests.Session, cred: Credentials, biz: str, *, count: int = 10,
                        delay: float = 2.0, timeout: float = 20.0, start_offset: int = 0) -> Iterator[HistoryItem]:
    if not cred.has_history_access(biz):
        raise HistoryError("no_credentials",
                           "缺少历史列表凭证，请先运行 `wxmp capture` 并在微信中打开该公众号的历史消息页面")
    offset = start_offset
    while True:
        method, url, params, headers = _build_getmsg(cred, biz, offset, count)
        resp = request_with_retry(session, method, url, params=params, headers=headers, timeout=timeout)
        try:
            data = resp.json()
        except json.JSONDecodeError:
            raise HistoryError("bad_response", f"历史接口未返回 JSON（凭证可能失效）：{resp.text[:200]!r}")
        ret = data.get("ret", 0)
        if ret != 0:
            raise HistoryError("expired" if ret in (-3, -6, -1) else "api_error",
                               f"历史接口返回 ret={ret} {data.get('errmsg', '')}（key 可能已过期或与 __biz 不匹配，请重新 capture）")
        raw_list = data.get("general_msg_list") or "{}"
        msgs = (json.loads(raw_list) if isinstance(raw_list, str) else raw_list).get("list") or []
        for msg in msgs:
            info = msg.get("comm_msg_info") or {}
            ext = msg.get("app_msg_ext_info")
            if not ext:
                continue
            item = _item_from_ext(biz, info, ext)
            if item:
                yield item
            for sub in ext.get("multi_app_msg_item_list") or []:
                item = _item_from_ext(biz, info, sub)
                if item:
                    yield item
        if not data.get("can_msg_continue") or not msgs:
            return
        offset = int(data.get("next_offset") or (offset + count))
        sleep_jitter(delay)


# ---------------------------------------------------------------------------
# 2. 公众平台后台
# ---------------------------------------------------------------------------
def _mp_headers(cred: Credentials) -> dict:
    return {
        "Cookie": cred.mp_cookie,
        "Referer": f"https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit&action=edit&type=10&token={cred.mp_token}&lang=zh_CN",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
    }


def _mp_check(data: dict, what: str) -> None:
    ret = (data.get("base_resp") or {}).get("ret", 0)
    if ret != 0:
        msg = (data.get("base_resp") or {}).get("err_msg", "")
        kind = "rate_limited" if ret == 200013 else ("expired" if ret in (200003, -6, 200002) else "api_error")
        raise HistoryError(kind, f"公众平台{what}接口返回 ret={ret} {msg}")


def mp_search_biz(session: requests.Session, cred: Credentials, query: str, timeout: float = 20.0) -> list[dict]:
    if not cred.has_mp_access():
        raise HistoryError("no_credentials", "缺少公众平台 token/cookie，请用 `wxmp mp-login` 写入")
    params = {"action": "search_biz", "token": cred.mp_token, "lang": "zh_CN", "f": "json", "ajax": "1",
              "random": str(random.random()), "query": query, "begin": "0", "count": "10"}
    resp = request_with_retry(session, "GET", MP_SEARCH_URL, params=params, headers=_mp_headers(cred), timeout=timeout)
    data = resp.json()
    _mp_check(data, "搜索")
    return [{"fakeid": b.get("fakeid"), "nickname": b.get("nickname"), "alias": b.get("alias"),
             "avatar": b.get("round_head_img"), "service_type": b.get("service_type")}
            for b in data.get("list") or []]


def iter_history_mp(session: requests.Session, cred: Credentials, fakeid: str, *, count: int = 5,
                    delay: float = 3.0, timeout: float = 20.0, begin: int = 0) -> Iterator[HistoryItem]:
    if not cred.has_mp_access():
        raise HistoryError("no_credentials", "缺少公众平台 token/cookie，请用 `wxmp mp-login` 写入")
    while True:
        params = {"token": cred.mp_token, "lang": "zh_CN", "f": "json", "ajax": "1", "action": "list_ex",
                  "begin": str(begin), "count": str(count), "query": "", "fakeid": fakeid, "type": "9"}
        resp = request_with_retry(session, "GET", MP_LIST_URL, params=params, headers=_mp_headers(cred), timeout=timeout)
        data = resp.json()
        _mp_check(data, "文章列表")
        items = data.get("app_msg_list") or []
        for a in items:
            url = normalize_url(a.get("link") or "")
            p = parse_url_params(url)
            yield HistoryItem(
                biz=p["biz"] or fakeid, mid=p["mid"] or str(a.get("aid", "")).split("_")[0],
                idx=p["idx"] or "1", title=a.get("title", ""), url=url,
                publish_time=int(a.get("create_time") or a.get("update_time") or 0),
                digest=a.get("digest", ""), cover_url=a.get("cover", ""), source="mp",
            )
        total = int(data.get("app_msg_cnt") or 0)
        begin += len(items)
        if not items or begin >= total:
            return
        sleep_jitter(delay)
