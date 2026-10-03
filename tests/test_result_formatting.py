"""结果格式化：零值、被丢掉的字段、以及不该重复的字段。

这里用**上游真实的模型对象**，不是自造的属性对象 ——
自造对象只能证明"格式化器认得上游字段"，证明不了"上游真的产出了那个字段"。

修掉的三类问题：

1. **零值被当成"没有值"**。``if item.episode`` 对 0 为假 —— 集数 0 直接消失；
   ``end_time if end_time else '?'`` 把 ``To=0`` 显示成问号。判断有无值要用 ``is not None``。
2. **同类信息在不同引擎下时有时无**。``similarity`` 只在 Iqdb 分支被读，
   而 SauceNAO 项明明也带这个字段；``author_url`` 同理。
   现在 ``similarity`` 不再按引擎分叉。
3. **没被读到的字段**。SauceNAO 项里还有 ``ext_urls`` 之外的信息（如 ``hidden``、
   ``index_name``），以及 TraceMoe 的 ``video`` / ``image`` —— 旧代码丢着没输出。
"""

from __future__ import annotations

from PicImageSearch.model.saucenao import SauceNAOItem
from PicImageSearch.model.tracemoe import TraceMoeItem
from PicImageSearch.model.yandex import YandexItem

from image_search_mcp.server import _format_result_item


def _trace_moe_item(**overrides) -> TraceMoeItem:
    payload = {
        "anilist": 12345, "filename": "ep01.mkv", "episode": 1,
        "from": 12.5, "to": 18.25, "similarity": 0.98,
        "video": "https://x/v.mp4", "image": "https://x/i.jpg",
    }
    payload.update(overrides)
    return TraceMoeItem(payload)


def _sauce_item(**data_overrides) -> SauceNAOItem:
    data = {
        "title": "SYNTHETIC_TITLE", "pixiv_id": 12345, "member_id": 67890,
        "creator": "SYNTHETIC_AUTHOR", "ext_urls": ["https://synthetic.invalid/original"],
    }
    data.update(data_overrides)
    return SauceNAOItem({
        "header": {"similarity": "95.21", "thumbnail": "https://t/thumb.jpg",
                   "index_id": 5, "index_name": "Pixiv"},
        "data": data,
    })


# ==========================================================================
# 零值
# ==========================================================================

def test_episode_zero_is_shown_not_swallowed():
    """``episode=0`` 是真值判断误伤的典型：0 是合法集数（如 OVA / 特别篇）。"""
    item = _trace_moe_item(episode=0)
    assert item.episode == 0
    output = _format_result_item(item, "TraceMoe")
    assert "Episode: 0" in output, f"集数 0 不应消失：{output!r}"


def test_episode_none_is_omitted():
    """真的没有集数时不该硬造一个 0 出来。"""
    item = _trace_moe_item()
    item.episode = None  # type: ignore[assignment]
    assert "Episode:" not in _format_result_item(item, "TraceMoe")


def test_end_time_zero_is_shown_not_replaced_by_question_mark():
    """``To=0`` 不应显示成 ``?`` —— 0 秒是合法时间点。"""
    item = _trace_moe_item(**{"from": 0.0, "to": 0.0})
    output = _format_result_item(item, "TraceMoe")
    assert "Time: 0.0s - 0.0s" in output, f"To=0 被吞掉了：{output!r}"
    assert "?" not in output


def test_end_time_none_still_shows_question_mark():
    """真正缺失的结束时间仍用 ``?`` 表示 —— 不能连这个也一起改掉。"""
    item = _trace_moe_item()
    item.To = None  # type: ignore[assignment]
    output = _format_result_item(item, "TraceMoe")
    assert "Time: 12.5s - ?s" in output


# ==========================================================================
# 被丢掉的字段
# ==========================================================================

def test_saucenao_similarity_is_reported():
    item = _sauce_item()
    assert item.similarity == 95.21
    assert "Similarity: 95.21%" in _format_result_item(item, "SauceNAO")


def test_saucenao_author_url_is_reported():
    item = _sauce_item()
    assert "Author URL: https://www.pixiv.net/users/67890" in _format_result_item(
        item, "SauceNAO"
    )


def test_similarity_is_not_engine_specific():
    """同一类信息不该"在 Iqdb 下有、在 SauceNAO 下没有"。"""
    item = _sauce_item()
    for engine in ("SauceNAO", "Iqdb", "AnyEngineName"):
        assert "Similarity:" in _format_result_item(item, engine), engine


def test_tracemoe_video_and_preview_are_reported():
    output = _format_result_item(_trace_moe_item(), "TraceMoe")
    assert "Video: https://x/v.mp4" in output
    assert "Preview: https://x/i.jpg" in output


def test_tracemoe_titles_that_exist_are_shown():
    item = _trace_moe_item()
    item.title_english = "SYNTHETIC_ENGLISH"
    item.title_romaji = "SYNTHETIC_ROMAJI"
    item.title_native = "SYNTHETIC_NATIVE"
    output = _format_result_item(item, "TraceMoe")
    for expected in ("SYNTHETIC_ENGLISH", "SYNTHETIC_ROMAJI", "SYNTHETIC_NATIVE"):
        assert expected in output


def test_tracemoe_chinese_title_is_read_when_upstream_provides_it():
    """中文标题字段照样读。

    上游当前**不会**返回它（``ANIME_INFO_QUERY`` 只请求 native/romaji/english），
    所以正常流程下这里恒为空。但读取逻辑留着：上游补上以后自动生效，
    不需要再改格式化器。这里只验证"有值就会输出"。
    """
    item = _trace_moe_item()
    item.title_chinese = "SYNTHETIC_CHINESE"
    assert "Chinese Title: SYNTHETIC_CHINESE" in _format_result_item(item, "TraceMoe")


def test_yandex_source_and_size_are_reported():
    item = YandexItem.__new__(YandexItem)
    item.title = ""  # type: ignore[attr-defined]
    item.url = "https://yandex.example/1"  # type: ignore[attr-defined]
    item.thumbnail = ""  # type: ignore[attr-defined]
    item.source = "example.com"  # type: ignore[attr-defined]
    item.content = "Some text"  # type: ignore[attr-defined]
    item.size = "1024x768"  # type: ignore[attr-defined]
    output = _format_result_item(item, "Yandex")
    assert "Source: example.com" in output
    assert "Size: 1024x768" in output


# ==========================================================================
# 不重复、不丢
# ==========================================================================

def test_url_appears_exactly_once():
    """``url`` 由通用分支输出；引擎分支不该再输出一遍。

    这条是防止"修一个不存在的缺陷"：如果以为 SauceNAO 的 url 丢了、
    于是在它的分支里补一次，结果就是同一行出现两遍。

    注意不能直接数 ``"URL: "``——``"Author URL: "`` 里也含这个子串，
    那样数出来的永远是 2。按"以它开头的行"来数。
    """
    output = _format_result_item(_sauce_item(), "SauceNAO")
    url_lines = [line for line in output.splitlines() if line.startswith("URL: ")]
    assert len(url_lines) == 1, f"URL 行应恰好一行，实际 {url_lines}"
    assert url_lines[0] == "URL: https://www.pixiv.net/artworks/12345"


def test_empty_item_produces_empty_string_not_crash():
    """上游偶尔会给出全空项（例如某引擎的占位结果）—— 不能抛异常。"""
    item = _trace_moe_item()
    for attr in ("title", "filename", "video", "image", "url"):
        if hasattr(item, attr):
            setattr(item, attr, "")
    assert isinstance(_format_result_item(item, "TraceMoe"), str)
