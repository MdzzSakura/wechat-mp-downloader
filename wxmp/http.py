"""HTTP 会话与带重试的请求。"""
from __future__ import annotations

import random
import time

import requests

from .config import Credentials, Settings

# 模拟 Windows 微信内置浏览器的 UA（抓到真实 UA 后会优先使用抓到的）
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) "
    "WindowsWechat(0x63090c11) XWEB/11253 Flue"
)


def make_session(settings: Settings, cred: Credentials | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (cred.user_agent if cred and cred.user_agent else DEFAULT_UA),
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    if cred:
        for k, v in cred.cookies.items():
            s.cookies.set(k, v, domain="mp.weixin.qq.com")
    if settings.proxy:
        s.proxies.update({"http": settings.proxy, "https": settings.proxy})
        s.verify = False  # 经过 mitmproxy 时证书是自签的
    s.timeout = settings.timeout  # type: ignore[attr-defined]
    return s


def sleep_jitter(base: float) -> None:
    """按基准秒数睡眠，附加 0.8~1.6 倍随机抖动（偏向更慢，避免规律性请求）。"""
    if base > 0:
        time.sleep(base * random.uniform(0.8, 1.6))


def request_with_retry(session: requests.Session, method: str, url: str, *, retries: int = 3,
                       timeout: float = 20.0, **kw) -> requests.Response:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = session.request(method, url, timeout=timeout, **kw)
            if resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            return resp
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    assert last is not None
    raise last
