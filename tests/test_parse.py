import json
from pathlib import Path

import pytest

from wxmp.article import Article, ArticleError, detect_error, js_var, parse_article, process_content
from wxmp.comments import normalize_comments
from wxmp.config import Credentials, RequestTemplate
from wxmp.storage import Store

FIXTURE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta property="og:title" content="测试标题 &amp; 符号" />
<meta property="og:description" content="摘要内容" />
<meta property="og:image" content="https://mmbiz.qpic.cn/cover.jpg" />
<meta name="author" content="小编" />
<title>忽略我</title></head><body>
<div id="js_name">测试公众号</div>
<div id="js_content" style="visibility: hidden; opacity: 0;">
<p>第一段 <strong>加粗</strong></p>
<img data-src="https://mmbiz.qpic.cn/a.jpg?wx_fmt=jpeg" data-type="jpeg" data-ratio="1" data-w="10" />
<img src="data:image/gif;base64,xxx" />
<iframe class="video_iframe" data-src="https://v.qq.com/iframe/preview.html?vid=1"></iframe>
<h2>小标题</h2><ul><li>甲</li><li>乙</li></ul>
</div>
<script>
var biz = ""||"MzI0MDAwMDAwMA==";
var mid = "" || "2247483653" * 1;
var idx = "" || "1" * 1;
var sn = "" || "abcdef123456";
var ct = "1700000000";
var comment_id = "" || "3141592653589" * 1;
var user_name = "gh_test123";
var msg_title = '测试标题 & 符号'.html(false);
window.appmsg_token = "tok_xyz";
</script></body></html>"""


def test_js_var():
    assert js_var(FIXTURE, "biz") == "MzI0MDAwMDAwMA=="
    assert js_var(FIXTURE, "mid") == "2247483653"
    assert js_var(FIXTURE, "comment_id") == "3141592653589"
    assert js_var(FIXTURE, "appmsg_token") == "tok_xyz"
    assert js_var(FIXTURE, "msg_title") == "测试标题 & 符号"
    assert js_var(FIXTURE, "not_exist") == ""


def test_parse_article_short_url():
    art = parse_article(FIXTURE, "https://mp.weixin.qq.com/s/shortcode")
    assert art.title == "测试标题 & 符号"
    assert art.biz == "MzI0MDAwMDAwMA=="
    assert (art.mid, art.idx, art.sn) == ("2247483653", "1", "abcdef123456")
    assert art.author == "小编"
    assert art.account_name == "测试公众号"
    assert art.gh_id == "gh_test123"
    assert art.publish_time == 1700000000
    assert art.comment_id == "3141592653589"
    assert art.canonical_url.startswith("https://mp.weixin.qq.com/s?__biz=MzI0MDAwMDAwMA==&mid=2247483653")
    assert art.key == "2247483653_1"


def test_parse_article_long_url_params_win():
    url = "https://mp.weixin.qq.com/s?__biz=BIZ&mid=1&idx=2&sn=SN&chksm=x"
    art = parse_article(FIXTURE, url)
    assert (art.biz, art.mid, art.idx, art.sn) == ("BIZ", "1", "2", "SN")


def test_process_content_without_download(tmp_path: Path):
    art = parse_article(FIXTURE, "https://mp.weixin.qq.com/s/x")
    process_content(art, FIXTURE, tmp_path, session=None, download_images=False)
    assert "visibility" not in art.content_html
    assert '<img' in art.content_html and 'data-original-src="https://mmbiz.qpic.cn/a.jpg?wx_fmt=jpeg"' in art.content_html
    assert "[视频]" in art.content_html and "<iframe" not in art.content_html
    assert art.images == [{"url": "https://mmbiz.qpic.cn/a.jpg?wx_fmt=jpeg", "path": None}]
    assert "## 小标题" in art.content_md
    assert "- 甲" in art.content_md
    assert "**加粗**" in art.content_md


def test_detect_error():
    with pytest.raises(ArticleError) as e:
        detect_error("<html>该内容已被发布者删除</html>")
    assert e.value.kind == "deleted"
    with pytest.raises(ArticleError) as e:
        detect_error("<html><div class='weui-msg'>环境异常</div></html>")
    assert e.value.kind == "captcha"
    detect_error('<div id="js_content"></div>')  # 不抛


def test_normalize_comments():
    data = {
        "elected_comment_total_cnt": 2,
        "elected_comment": [
            {"content_id": "c1", "nick_name": "A", "logo_url": "", "content": "顶层1", "create_time": 1700000001,
             "like_num": 3, "reply_new": {"reply_list": [
                 {"reply_id": 11, "nick_name": "作者", "content": "回复A", "create_time": 1700000002, "is_from": 1, "reply_like_num": 1}
             ]}},
            {"content_id": "c2", "nick_name": "B", "content": "顶层2", "create_time": 1700000003, "like_num": "0",
             "reply": {"reply_list": [{"content": "老结构回复", "create_time": 1700000004}]}},
        ],
        "my_comment": [{"content_id": "m1", "nick_name": "我", "content": "我的留言", "create_time": 1}],
    }
    cs = normalize_comments(data)
    assert [c.content_id for c in cs] == ["c1", "11", "c2", "c2-r0", "m1"]
    assert cs[1].parent_content_id == "c1" and cs[1].is_author and cs[1].like_num == 1
    assert cs[3].parent_content_id == "c2" and not cs[3].is_author
    assert cs[4].is_elected is False and cs[0].is_elected is True


def test_credentials_roundtrip(tmp_path: Path):
    p = tmp_path / "cred.json"
    c = Credentials(biz="B", uin="U", appmsg_token="T", pass_ticket="P", cookies={"wap_sid2": "x"})
    c.templates["getcomment"] = RequestTemplate("GET", "https://mp.weixin.qq.com/mp/appmsg_comment",
                                                {"action": "getcomment"}, {"Cookie": "a=b"})
    c.save(p)
    c2 = Credentials.load(p)
    assert c2.biz == "B" and c2.cookies == {"wap_sid2": "x"}
    assert c2.templates["getcomment"].query == {"action": "getcomment"}
    assert c2.has_comment_access()


def test_store_roundtrip(tmp_path: Path):
    st = Store(tmp_path / "t.db")
    art = parse_article(FIXTURE, "https://mp.weixin.qq.com/s/x")
    process_content(art, FIXTURE, tmp_path, session=None, download_images=False)
    aid = st.upsert_article(art, tmp_path / "dir")
    # 同一篇文章用长链接再次写入 -> 更新而不是新增
    art2 = parse_article(FIXTURE, art.canonical_url)
    assert st.upsert_article(art2, tmp_path / "dir") == aid
    assert st.stats()["articles_ok"] == 1
    cs = normalize_comments({"elected_comment": [{"content_id": "c1", "content": "x", "create_time": 1}]})
    st.replace_comments(aid, cs)
    row = st.get_article(aid)
    assert row["comment_count"] == 1 and row["comments_fetched_at"]
    assert st.articles_pending_comments() == []
    out = tmp_path / "e.json"
    assert st.export(out) == 1
    assert json.loads(out.read_text("utf-8"))[0]["comments"][0]["content_id"] == "c1"
    st.mark_failed("https://mp.weixin.qq.com/s/gone", "deleted", "x")
    assert st.stats()["articles_failed"] == 1
    st.close()
