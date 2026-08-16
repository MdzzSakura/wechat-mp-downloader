"""内置 mitmproxy：自动从微信客户端流量中捕获凭证与请求模板。

用法：`wxmp capture`，然后在微信里打开任意一篇公众号文章（下拉到评论区），
或打开某公众号的“历史消息 / 消息列表”页面即可。

捕获内容：
- 任意 mp.weixin.qq.com 请求中的 __biz / uin / key / pass_ticket / appmsg_token 等 query 参数
- Cookie（wxuin / pass_ticket / wap_sid2 ...）与 User-Agent
- 评论接口 /mp/appmsg_comment?action=getcomment 的完整请求模板
- 历史列表接口 /mp/profile_ext?action=getmsg 的完整请求模板（按 __biz 各存一份）
"""
from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path
from typing import Callable

from .config import CRED_QUERY_KEYS, Credentials, RequestTemplate

WX_HOST = "mp.weixin.qq.com"
_DROP_HEADERS = {"host", "content-length", "connection", "proxy-connection", "accept-encoding", "transfer-encoding"}
_INTERESTING_COOKIES = ("wxuin", "pass_ticket", "wap_sid2", "rewardsn", "wxtokenkey", "pgv_pvid", "devicetype", "version", "lang", "wxuin")


class WxCaptureAddon:
    """mitmproxy addon。每捕获到新信息就写回 credentials.json。"""

    def __init__(self, cred_path: Path, log: Callable[[str], None] = print, install_cert: bool = False):
        self.cred_path = cred_path
        self.cred = Credentials.load(cred_path)
        self.log = log
        self.install_cert = install_cert
        self.seen: set[str] = set()

    # mitmproxy 生命周期钩子 -------------------------------------------
    def running(self):
        if self.install_cert:
            cer = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.cer"
            if cer.exists():
                r = subprocess.run(["certutil", "-addstore", "-user", "Root", str(cer)],
                                   capture_output=True, text=True)
                self.log("[cert] 已安装 mitmproxy 根证书到当前用户受信任根" if r.returncode == 0
                         else f"[cert] 安装证书失败：{r.stdout or r.stderr}")
            else:
                self.log(f"[cert] 未找到证书文件 {cer}，请稍后手动安装")

    def request(self, flow):  # noqa: C901 - 单一入口更直观
        req = flow.request
        if req.pretty_host != WX_HOST:
            return
        query = {k: v for k, v in req.query.items() if v}
        changed: list[str] = []

        for k in CRED_QUERY_KEYS:
            v = query.get(k)
            if not v:
                continue
            attr = "biz" if k == "__biz" else k
            if getattr(self.cred, attr) != v:
                setattr(self.cred, attr, v)
                changed.append(attr)

        ua = req.headers.get("User-Agent", "")
        if ua and ua != self.cred.user_agent:
            self.cred.user_agent = ua
            changed.append("user_agent")

        for ck, cv in req.cookies.items():
            if cv and self.cred.cookies.get(ck) != cv:
                self.cred.cookies[ck] = cv
                changed.append(f"cookie:{ck}")

        path = req.path.split("?", 1)[0]
        action = query.get("action", "")
        tpl_name = None
        if path == "/mp/appmsg_comment" and action == "getcomment":
            tpl_name = "getcomment"
        elif path == "/mp/profile_ext" and action == "getmsg":
            tpl_name = "getmsg"
        elif path == "/mp/getappmsgext":  # 阅读数/点赞数接口，顺便留存
            tpl_name = "getappmsgext"

        if tpl_name:
            headers = {k: v for k, v in req.headers.items() if k.lower() not in _DROP_HEADERS}
            body = req.get_text(strict=False) if req.content else None
            tpl = RequestTemplate(method=req.method, url=f"{req.scheme}://{req.pretty_host}{path}",
                                  query=query, headers=headers, body=body, captured_at=time.time())
            self.cred.templates[tpl_name] = tpl
            changed.append(f"template:{tpl_name}")
            biz = query.get("__biz")
            if tpl_name == "getmsg" and biz:
                self.cred.templates[f"getmsg:{biz}"] = tpl
                changed.append(f"template:getmsg:{biz}")

        if changed:
            self.cred.updated_at = time.time()
            self.cred.save(self.cred_path)
            new = [c for c in changed if c not in self.seen]
            self.seen.update(changed)
            if new:
                self.log(f"[capture] 已更新凭证: {', '.join(new)}")
            if tpl_name:
                self.log(f"[capture] 抓到 {tpl_name} 请求模板 (biz={query.get('__biz', '?')})")


async def run_proxy(addon: WxCaptureAddon, host: str = "127.0.0.1", port: int = 8080) -> None:
    from mitmproxy.options import Options
    from mitmproxy.tools.dump import DumpMaster

    opts = Options(listen_host=host, listen_port=port, ssl_insecure=True)
    master = DumpMaster(opts, with_termlog=False, with_dumper=False)
    master.addons.add(addon)
    try:
        await master.run()
    finally:
        master.shutdown()


def start_capture(cred_path: Path, port: int = 8080, log: Callable[[str], None] = print,
                  install_cert: bool = False) -> None:
    addon = WxCaptureAddon(cred_path, log=log, install_cert=install_cert)
    try:
        asyncio.run(run_proxy(addon, port=port))
    except KeyboardInterrupt:
        pass
