# wxmp — 微信公众号文章 / 评论下载器

把公众号文章（正文、图片）和留言评论下载到本地并结构化存储：

- 正文：`article.md`（Markdown，含 front-matter）、`article.html`（可离线打开）、`raw.html`（原始页面，便于重新解析）
- 图片：`images/001.jpg ...`，Markdown/HTML 中已改为相对路径
- 评论：`comments.json`
- 元数据：`meta.json`
- 全部元数据 + 正文 + 评论 + 图片索引同时写入 SQLite（`data/wxmp.db`），可用 `wxmp export` 导出 JSON/CSV

支持两种来源：**给定 URL 列表** 和 **按公众号批量抓取全部历史文章**（增量、可断点续跑）。

## 安装

```bash
cd wechat-mp-downloader
pip install -e .
wxmp --help
```

依赖 Python ≥ 3.10。`mitmproxy` 仅在 `wxmp capture` 时用到。

## 快速开始

### 1. 只下载正文（不需要任何凭证）

```bash
wxmp download "https://mp.weixin.qq.com/s/xxxxxxxx" "https://mp.weixin.qq.com/s?__biz=...&mid=...&idx=1&sn=..."
wxmp download -f urls.txt          # 每行一个 URL，# 开头忽略
```

### 2. 捕获凭证（评论、按公众号抓取都需要）

评论接口和历史文章接口必须带微信客户端的登录凭证（`uin / key / pass_ticket / appmsg_token / wap_sid2`），
工具内置了 mitmproxy 自动捕获：

```bash
wxmp capture --install-cert       # 首次加 --install-cert 安装 mitmproxy 根证书；默认会自动设置并在退出时恢复 Windows 系统代理
```

然后在 **微信 PC 版** 里：

1. 打开任意一篇公众号文章，滑到底部 **评论区**（会看到 `抓到 getcomment 请求模板`）；
2. 想按公众号批量下载：进入该公众号主页 → **消息 / 历史文章列表**，往下翻一页（会看到 `抓到 getmsg 请求模板 (biz=...)`）。

看到提示后 `Ctrl+C` 退出。凭证写在 `data/credentials.json`，一般 **20~30 分钟失效**，失效后重新 capture 即可（`wxmp status` 可查看）。

> 微信 PC 版走系统代理；若微信没有流量经过代理，请检查系统代理是否被其他软件（如 Clash）覆盖，或用 `wxmp proxy on/off/show` 手动处理。
> 首次抓 HTTPS 需要信任 mitmproxy 证书：`--install-cert` 会执行 `certutil -addstore -user Root ~/.mitmproxy/mitmproxy-ca-cert.cer`。

### 3. 下载评论

```bash
wxmp download <url>               # 有凭证时自动带上评论
wxmp comments --pending           # 给之前没抓到评论的文章补抓
wxmp comments <url> --force       # 重新抓某篇的评论
```

### 4. 按公众号批量抓取

```bash
wxmp crawl                         # 默认用最近一次 capture 到的公众号 (biz)
wxmp crawl --biz MzA5MzYyNzQ0MQ== --since 2024-01-01 --max 50
wxmp crawl --list-only             # 只保存文章清单到 history 表，不下载正文
```

`crawl` 会先翻页拉取历史清单存入 `history` 表，再逐篇下载；已下载的自动跳过，可以随时中断再跑。

另一种历史清单来源：如果你有自己的公众号，可以用公众平台后台接口（更稳定，不依赖客户端 key）：

```bash
wxmp mp-login --token 123456 --cookie "..."   # 浏览器登录 mp.weixin.qq.com 后从地址栏 / DevTools 拷贝
wxmp mp-search 公众号名称                      # 查 fakeid
wxmp crawl --source mp --fakeid MzA5MzYyNzQ0MQ==
```

### 5. 查看 / 导出

```bash
wxmp status
wxmp list --limit 20
wxmp export --format json -o out.json --with-content
wxmp export --format csv
```

## 目录结构

```
data/
├── wxmp.db                     # SQLite：accounts / articles / comments / images / history
├── credentials.json            # capture 抓到的凭证与请求模板（含登录态，勿外传）
└── articles/<公众号>/<日期>_<标题>_<mid>_<idx>/
    ├── article.md
    ├── article.html
    ├── raw.html
    ├── meta.json
    ├── comments.json
    └── images/001.jpg ...
```

`comments` 表中 `parent_content_id` 为空表示顶层留言，否则为该留言下的回复；`is_author=1` 表示作者回复；`is_elected=0` 为“我的留言”等非精选留言。

## 常用参数

- `--data-dir DIR`：数据目录（默认 `./data`，或环境变量 `WXMP_DATA_DIR`）
- `--delay N`：请求间隔基准秒数（默认 2，会随机抖动；批量抓取建议 3~5，遇到“环境异常/操作频繁”就调大）
- `--proxy http://127.0.0.1:8080`：让下载流量也经过 mitmproxy，便于调试

## 实现说明

- 文章页无需登录即可获取；标题/作者/时间/`comment_id`/`biz|mid|idx|sn` 从 `og:*` meta 与页面脚本变量中解析。
- 评论接口 `/mp/appmsg_comment?action=getcomment` 与历史接口 `/mp/profile_ext?action=getmsg` 回放的是 capture 时抓到的 **真实请求模板**（只替换 `__biz/appmsgid/idx/comment_id/offset` 等关键参数），因此对客户端版本变化更健壮；没有模板时退化为按已知参数拼请求。
- 历史接口的 `key` 通常与 `__biz` 绑定，工具按 `getmsg:<biz>` 分别保存模板；换公众号需要在微信里再打开一次它的历史页。
- 微信有频控与验证码（“环境异常”），检测到后会自动停止本轮批量下载。

## 开发

```bash
pip install -e ".[dev]"
pytest
```
