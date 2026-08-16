"""运行参数与凭证的读写。

凭证（credentials.json）由 `wxmp capture` 通过 mitmproxy 从微信客户端自动抓取，
其中除了 uin/key/pass_ticket/appmsg_token 等散参数外，还会保存几条“真实请求模板”
（评论接口 getcomment、历史列表接口 getmsg）。回放时只替换关键参数，最大程度
避免因客户端版本变化导致的参数缺失。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Settings:
    """命令行 / 环境变量给出的运行参数（不含凭证）。"""

    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("WXMP_DATA_DIR", "data")))
    delay: float = 6.0      # 请求间隔基准（秒），实际会附加随机抖动（0.8~1.6 倍）
    rest_every: int = 15    # 每连续下载多少篇文章后长休息一次（0=不休息）
    rest_seconds: float = 120.0  # 长休息基准秒数（同样带随机抖动）
    timeout: float = 20.0
    proxy: str | None = None  # 例如 http://127.0.0.1:8080，可让下载流量也经过 mitmproxy 便于调试

    @property
    def db_path(self) -> Path:
        return self.data_dir / "wxmp.db"

    @property
    def credentials_path(self) -> Path:
        return self.data_dir / "credentials.json"

    @property
    def articles_dir(self) -> Path:
        return self.data_dir / "articles"


@dataclass
class RequestTemplate:
    """从微信客户端抓到的一条真实请求。回放时仅替换关键 query 参数。"""

    method: str
    url: str                       # 不含 query 的 URL
    query: dict[str, str]
    headers: dict[str, str]
    body: str | None = None
    captured_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RequestTemplate":
        return cls(**d)


# 会从任意 mp.weixin.qq.com 请求的 query 中提取并保存的字段
CRED_QUERY_KEYS = (
    "__biz", "uin", "key", "pass_ticket", "appmsg_token", "wxtoken",
    "devicetype", "clientversion", "version",
)

# 凭证大致有效期（秒）。appmsg_token / key 一般 20~30 分钟左右失效。
CRED_TTL = 25 * 60


@dataclass
class Credentials:
    biz: str = ""                 # 最近一次在客户端里打开的公众号 __biz
    uin: str = ""
    key: str = ""
    pass_ticket: str = ""
    appmsg_token: str = ""
    wxtoken: str = ""
    devicetype: str = ""
    clientversion: str = ""
    version: str = ""
    user_agent: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    templates: dict[str, RequestTemplate] = field(default_factory=dict)
    # 公众平台后台（mp.weixin.qq.com 登录态），可选的另一种历史文章列表来源
    mp_token: str = ""
    mp_cookie: str = ""
    updated_at: float = 0.0

    # ---- 持久化 -------------------------------------------------------
    @classmethod
    def load(cls, path: Path) -> "Credentials":
        if not path.exists():
            return cls()
        d = json.loads(path.read_text("utf-8"))
        tpls = {k: RequestTemplate.from_dict(v) for k, v in d.pop("templates", {}).items()}
        known = set(cls.__dataclass_fields__)
        cred = cls(**{k: v for k, v in d.items() if k in known})
        cred.templates = tpls
        return cred

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        d = asdict(self)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, path)

    # ---- 状态判断 -----------------------------------------------------
    def age(self) -> float | None:
        return None if not self.updated_at else time.time() - self.updated_at

    def is_stale(self) -> bool:
        a = self.age()
        return a is None or a > CRED_TTL

    def has_comment_access(self) -> bool:
        return "getcomment" in self.templates or bool(self.appmsg_token and self.pass_ticket)

    def has_history_access(self, biz: str = "") -> bool:
        if biz and f"getmsg:{biz}" in self.templates:
            return True
        return "getmsg" in self.templates or bool(self.key and self.pass_ticket and self.uin)

    def has_mp_access(self) -> bool:
        return bool(self.mp_token and self.mp_cookie)

    def summary(self) -> dict:
        a = self.age()
        return {
            "updated": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.updated_at)) if self.updated_at else "-",
            "age_min": round(a / 60, 1) if a is not None else None,
            "stale": self.is_stale(),
            "biz": self.biz,
            "uin": self.uin,
            "has_key": bool(self.key),
            "has_pass_ticket": bool(self.pass_ticket),
            "has_appmsg_token": bool(self.appmsg_token),
            "cookies": sorted(self.cookies),
            "templates": sorted(self.templates),
            "mp_backend": self.has_mp_access(),
        }
