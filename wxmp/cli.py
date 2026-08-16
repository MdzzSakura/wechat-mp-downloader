"""命令行入口：wxmp"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import Credentials, Settings
from .downloader import Downloader, DownloadResult, fmt_ts
from .storage import Store

console = Console()


class Ctx:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._store: Store | None = None
        self._cred: Credentials | None = None

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = Store(self.settings.db_path)
        return self._store

    @property
    def cred(self) -> Credentials:
        if self._cred is None:
            self._cred = Credentials.load(self.settings.credentials_path)
        return self._cred

    def downloader(self) -> Downloader:
        return Downloader(self.settings, self.store, self.cred, log=lambda m: console.print(f"[yellow]{m}[/]"))


pass_ctx = click.make_pass_decorator(Ctx)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="wxmp")
@click.option("--data-dir", type=click.Path(path_type=Path), default=None,
              help="数据目录（默认 ./data 或环境变量 WXMP_DATA_DIR）")
@click.option("--delay", type=float, default=2.0, show_default=True, help="请求间隔基准秒数")
@click.option("--timeout", type=float, default=20.0, show_default=True)
@click.option("--proxy", default=None, help="下载流量使用的 HTTP 代理，如 http://127.0.0.1:8080")
@click.pass_context
def main(ctx: click.Context, data_dir: Path | None, delay: float, timeout: float, proxy: str | None) -> None:
    """微信公众号文章 / 评论下载器：正文存 Markdown+HTML+图片，元数据与评论存 SQLite。"""
    s = Settings(delay=delay, timeout=timeout, proxy=proxy)
    if data_dir:
        s.data_dir = data_dir
    ctx.obj = Ctx(s)


# ---------------------------------------------------------------------------
# capture / proxy
# ---------------------------------------------------------------------------
@main.command()
@click.option("--port", default=8080, show_default=True)
@click.option("--set-system-proxy/--no-set-system-proxy", default=True, show_default=True,
              help="自动把 Windows 系统代理指向本工具（退出时恢复）")
@click.option("--install-cert", is_flag=True, help="自动把 mitmproxy 根证书装到当前用户受信任根（首次需要）")
@pass_ctx
def capture(c: Ctx, port: int, set_system_proxy: bool, install_cert: bool) -> None:
    """启动内置代理，自动从微信客户端流量捕获评论 / 历史接口凭证。"""
    from .capture import start_capture

    old = None
    if set_system_proxy:
        try:
            from .winproxy import set_proxy
            old = set_proxy(f"127.0.0.1:{port}")
            console.print(f"[green]已设置系统代理 -> 127.0.0.1:{port}[/]（Ctrl+C 退出时自动恢复）")
        except Exception as e:  # 非 Windows 或无权限
            console.print(f"[yellow]未能自动设置系统代理：{e}，请手动设置 HTTP 代理 127.0.0.1:{port}[/]")

    console.rule("wxmp capture")
    console.print(f"代理监听 127.0.0.1:{port}，凭证写入 {c.settings.credentials_path}")
    console.print("请在 [bold]微信 PC 版[/] 中：")
    console.print("  1) 打开任意一篇公众号文章并 [bold]滑到底部评论区[/]  -> 抓到评论接口凭证")
    console.print("  2) 打开目标公众号主页 -> [bold]消息 / 历史文章列表[/] 并往下翻一页 -> 抓到该公众号历史接口凭证")
    console.print("首次使用需信任 mitmproxy 证书：加 --install-cert，或手动安装 ~/.mitmproxy/mitmproxy-ca-cert.cer")
    console.print("看到 “已更新凭证 / 抓到 xxx 请求模板” 后 Ctrl+C 退出即可。\n")
    try:
        start_capture(c.settings.credentials_path, port=port,
                      log=lambda m: console.print(f"[cyan]{m}[/]"), install_cert=install_cert)
    finally:
        if old is not None:
            from .winproxy import restore_proxy
            restore_proxy(old)
            console.print("[green]已恢复系统代理设置[/]")


@main.command()
@click.argument("state", type=click.Choice(["on", "off", "show"]))
@click.option("--server", default="127.0.0.1:8080", show_default=True)
def proxy(state: str, server: str) -> None:
    """手动开/关 Windows 系统代理（capture 异常退出未恢复时可用 off）。"""
    from .winproxy import get_proxy, set_proxy
    if state == "show":
        console.print(get_proxy())
    else:
        set_proxy(server if state == "on" else None)
        console.print(f"系统代理已{'开启 -> ' + server if state == 'on' else '关闭'}")


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------
def _print_result(i: int, total: int, r: DownloadResult) -> None:
    prefix = f"[{i}/{total}]"
    if not r.ok:
        console.print(f"[red]{prefix} 失败[/] {r.url}\n      {r.error}")
        return
    if r.skipped:
        line = f"[dim]{prefix} 已存在，跳过[/] {r.url}"
    else:
        a = r.article
        line = f"[green]{prefix} OK[/] {a.title!r} · {a.account_name} · {fmt_ts(a.publish_time)} · 图片 {len(a.images)}"
    if r.comment_count is not None:
        line += f" · 评论 {r.comment_count}"
    if r.comment_error:
        line += f"\n      [yellow]评论未抓取：{r.comment_error}[/]"
    console.print(line)
    if r.dir_path and not r.skipped:
        console.print(f"      -> {r.dir_path}")


def _read_urls(urls: tuple[str, ...], file: Path | None) -> list[str]:
    out = list(urls)
    if file:
        for line in file.read_text("utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line.split()[0])
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


@main.command()
@click.argument("urls", nargs=-1)
@click.option("-f", "--file", type=click.Path(exists=True, path_type=Path), help="URL 列表文件（每行一个，# 开头忽略）")
@click.option("--comments/--no-comments", default=True, show_default=True)
@click.option("--images/--no-images", default=True, show_default=True)
@click.option("--force", is_flag=True, help="已存在也重新下载")
@pass_ctx
def download(c: Ctx, urls: tuple[str, ...], file: Path | None, comments: bool, images: bool, force: bool) -> None:
    """按 URL 下载文章（含图片、评论）。"""
    targets = _read_urls(urls, file)
    if not targets:
        raise click.UsageError("请提供至少一个 URL 或 -f 文件")
    if comments and not c.cred.has_comment_access():
        console.print("[yellow]提示：尚未捕获评论接口凭证，本次只下载正文；运行 `wxmp capture` 后可用 `wxmp comments --pending` 补抓[/]")
    elif comments and c.cred.is_stale():
        console.print("[yellow]提示：凭证距上次捕获已超过 25 分钟，评论接口可能已失效[/]")
    d = c.downloader()
    results = d.download_many(targets, with_comments=comments, with_images=images, force=force, on_result=_print_result)
    ok = sum(1 for r in results if r.ok)
    console.print(f"\n完成：成功 {ok} / 失败 {len(results) - ok} / 总计 {len(results)}")


# ---------------------------------------------------------------------------
# crawl (按公众号)
# ---------------------------------------------------------------------------
def _parse_since(s: str | None) -> int | None:
    if not s:
        return None
    return int(time.mktime(time.strptime(s, "%Y-%m-%d")))


@main.command()
@click.option("--biz", default=None, help="公众号 __biz（默认使用最近一次 capture 到的）")
@click.option("--source", type=click.Choice(["wechat", "mp"]), default="wechat", show_default=True,
              help="历史列表来源：wechat=微信客户端历史接口；mp=公众平台后台")
@click.option("--fakeid", default=None, help="source=mp 时的 fakeid（等同 __biz，可用 mp-search 查询）")
@click.option("--max", "max_n", type=int, default=0, help="最多下载多少篇（0=不限）")
@click.option("--since", default=None, help="只下载该日期之后发布的文章，格式 2024-01-01")
@click.option("--list-only", is_flag=True, help="只抓取并保存文章清单，不下载正文")
@click.option("--comments/--no-comments", default=True, show_default=True)
@click.option("--images/--no-images", default=True, show_default=True)
@pass_ctx
def crawl(c: Ctx, biz: str | None, source: str, fakeid: str | None, max_n: int, since: str | None,
          list_only: bool, comments: bool, images: bool) -> None:
    """按公众号抓取历史文章清单并逐篇下载（支持增量：已下载的会跳过）。"""
    from .history import HistoryError, iter_history_mp, iter_history_wechat

    biz = biz or fakeid or c.cred.biz
    if not biz:
        raise click.UsageError("请指定 --biz，或先运行 `wxmp capture` 并在微信中打开该公众号")
    since_ts = _parse_since(since)
    d = c.downloader()

    console.rule(f"抓取历史清单 biz={biz} source={source}")
    n_new = 0
    try:
        it = (iter_history_mp(d.session, c.cred, biz, delay=c.settings.delay, timeout=c.settings.timeout)
              if source == "mp" else
              iter_history_wechat(d.session, c.cred, biz, delay=c.settings.delay, timeout=c.settings.timeout))
        batch = []
        stop = False
        for item in it:
            if since_ts and item.publish_time and item.publish_time < since_ts:
                stop = True   # 列表按时间倒序，早于 since 即可停止
                break
            batch.append(item)
            n_new += 1
            console.print(f"  [dim]{fmt_ts(item.publish_time)}[/] {item.title}")
            if len(batch) >= 20:
                c.store.add_history(batch)
                batch = []
            if max_n and n_new >= max_n:
                break
        if batch:
            c.store.add_history(batch)
        if stop:
            console.print(f"[dim]已到达 --since {since}，停止翻页[/]")
    except HistoryError as e:
        console.print(f"[red]历史清单抓取中断：[{e.kind}] {e}[/]")
        if e.kind in ("expired", "no_credentials", "bad_response"):
            console.print("[yellow]请重新运行 `wxmp capture`，并在微信中打开该公众号的历史消息页面翻一页[/]")
    console.print(f"清单：本次获取 {n_new} 篇，库中该公众号共 {c.store.history_count(biz)} 篇")
    if list_only:
        return

    pending = c.store.history_not_downloaded(biz, since=since_ts)
    if max_n:
        pending = pending[:max_n]
    if not pending:
        console.print("没有需要下载的新文章")
        return
    console.rule(f"下载 {len(pending)} 篇")
    if comments and not c.cred.has_comment_access():
        console.print("[yellow]提示：尚未捕获评论接口凭证，只下载正文[/]")
    results = d.download_many([r["url"] for r in pending], with_comments=comments, with_images=images,
                              on_result=_print_result)
    ok = sum(1 for r in results if r.ok)
    console.print(f"\n完成：成功 {ok} / 失败 {len(results) - ok} / 总计 {len(results)}")


@main.command("mp-login")
@click.option("--token", required=True, help="公众平台后台 URL 中的 token=xxxx")
@click.option("--cookie", required=True, help="浏览器里 mp.weixin.qq.com 的完整 Cookie 字符串")
@pass_ctx
def mp_login(c: Ctx, token: str, cookie: str) -> None:
    """写入公众平台后台（mp.weixin.qq.com）登录态，用于 --source mp。"""
    c.cred.mp_token = token
    c.cred.mp_cookie = cookie
    c.cred.save(c.settings.credentials_path)
    console.print("[green]已保存公众平台 token / cookie[/]")


@main.command("mp-search")
@click.argument("query")
@pass_ctx
def mp_search(c: Ctx, query: str) -> None:
    """通过公众平台后台按名称搜索公众号，得到 fakeid（__biz）。"""
    from .history import HistoryError, mp_search_biz
    try:
        rows = mp_search_biz(c.downloader().session, c.cred, query, timeout=c.settings.timeout)
    except HistoryError as e:
        raise click.ClickException(f"[{e.kind}] {e}")
    t = Table("fakeid", "名称", "微信号", "类型")
    for r in rows:
        t.add_row(r["fakeid"] or "", r["nickname"] or "", r["alias"] or "", str(r["service_type"]))
    console.print(t)


# ---------------------------------------------------------------------------
# comments / list / export / status
# ---------------------------------------------------------------------------
@main.command()
@click.argument("urls", nargs=-1)
@click.option("--pending", is_flag=True, help="补抓所有还没抓过评论的文章")
@click.option("--biz", default="", help="配合 --pending 限定公众号")
@click.option("--force", is_flag=True, help="已抓过的也重新抓")
@pass_ctx
def comments(c: Ctx, urls: tuple[str, ...], pending: bool, biz: str, force: bool) -> None:
    """单独（重新）抓取文章评论。"""
    if not c.cred.has_comment_access():
        raise click.ClickException("尚未捕获评论接口凭证，请先运行 `wxmp capture`")
    if c.cred.is_stale():
        console.print("[yellow]提示：凭证已超过 25 分钟，可能失效[/]")
    d = c.downloader()
    rows = []
    if pending:
        rows = c.store.articles_pending_comments(biz)
    for u in urls:
        row = c.store.find_article(u)
        if not row:
            r = d.download(u, with_comments=True)
            _print_result(1, 1, r)
            continue
        if row["comments_fetched_at"] and not force:
            console.print(f"[dim]已抓过评论（{row['comment_count']} 条），加 --force 重抓：{u}[/]")
            continue
        rows.append(row)
    if not rows and not urls:
        console.print("没有待抓取的文章")
        return
    for i, row in enumerate(rows, 1):
        r = d.refetch_comments(row)
        title = row["title"] or row["url"]
        if r.comment_error:
            console.print(f"[yellow][{i}/{len(rows)}] {title}: {r.comment_error}[/]")
            if "[expired]" in r.comment_error or "[bad_response]" in r.comment_error:
                console.print("[red]凭证已失效，停止。请重新 `wxmp capture`[/]")
                break
        else:
            console.print(f"[green][{i}/{len(rows)}] {title}: 评论 {r.comment_count}[/]")
        if i < len(rows):
            time.sleep(c.settings.delay / 2)


@main.command("list")
@click.option("--biz", default="")
@click.option("--status", default="", help="ok / deleted / blocked / captcha / error ...")
@click.option("--limit", default=50, show_default=True)
@pass_ctx
def list_cmd(c: Ctx, biz: str, status: str, limit: int) -> None:
    """列出已下载的文章。"""
    rows = c.store.list_articles(biz=biz, status=status, limit=limit)
    t = Table("id", "发布时间", "公众号", "标题", "评论", "状态", show_lines=False)
    for r in rows:
        t.add_row(str(r["id"]), fmt_ts(r["publish_time"]), r["account_name"] or "",
                  (r["title"] or r["url"])[:50], str(r["comment_count"] or 0), r["status"] or "")
    console.print(t)
    console.print(c.store.stats())


@main.command()
@click.option("--format", "fmt", type=click.Choice(["json", "csv"]), default="json", show_default=True)
@click.option("-o", "--out", type=click.Path(path_type=Path), default=None)
@click.option("--biz", default="")
@click.option("--with-content", is_flag=True, help="JSON 中包含正文 HTML / Markdown")
@pass_ctx
def export(c: Ctx, fmt: str, out: Path | None, biz: str, with_content: bool) -> None:
    """导出文章（含评论）为 JSON / CSV。"""
    out = out or (c.settings.data_dir / f"export.{fmt}")
    n = c.store.export(out, fmt=fmt, biz=biz, with_content=with_content)
    console.print(f"已导出 {n} 篇 -> {out}")


@main.command()
@pass_ctx
def status(c: Ctx) -> None:
    """查看凭证状态与数据库统计。"""
    console.rule("凭证")
    console.print(c.cred.summary())
    if c.cred.is_stale():
        console.print("[yellow]凭证过期或缺失：需要评论 / 历史列表时请运行 `wxmp capture`[/]")
    console.rule("数据库")
    console.print(c.store.stats())
    console.print(f"数据目录：{c.settings.data_dir.resolve()}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
