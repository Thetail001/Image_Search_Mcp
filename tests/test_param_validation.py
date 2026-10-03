"""``extra_params`` 校验行为。

重心是**拒绝必须发生在读文件与建网络之前**。

早期版本的写法是"先把 ``file``/``url`` 装好，再 ``search_kwargs.update(extra_params)``"，
于是调用方可以用 ``{"file": "/etc/passwd"}`` 覆盖输入，而 ``PicImageSearch.utils.read_file()``
接受路径字符串并直接 ``open()`` —— 服务端任意文件读取，读出来的内容还会被
multipart 上传到搜索引擎站点。两组对照（正例越权成功、负例在读取函数处失败）
在本机与远端各证书过一次。

所以这里不仅断言"被拒绝"，还断言**没有产生任何出站请求** —— 但注意
"没有出站请求"本身**不足以**证明没读文件（读完再拒也满足它）。
真正证明"在读取之前拒绝"的是 ``test_reserved_file_is_rejected_before_any_read``：
把读文件函数换成会记录的哨兵，断言它从未被调用。
"""

from __future__ import annotations

import httpx
import pytest

from image_search_mcp import params


def test_none_returns_empty():
    assert params.validate_extra_params("Yandex", None) == ({}, {})


def test_empty_object_returns_empty():
    assert params.validate_extra_params("Yandex", {}) == ({}, {})


@pytest.mark.parametrize("bad", [[], "x", 1, True, 3.5])
def test_top_level_must_be_object(bad):
    """数组、标量、布尔都不是合法的顶层形态。"""
    with pytest.raises(params.ParamError, match="顶层必须是对象"):
        params.validate_extra_params("Yandex", bad)


@pytest.mark.parametrize("key", sorted(params.RESERVED_KEYS))
def test_every_reserved_key_is_rejected(key: str):
    with pytest.raises(params.ParamError, match="保留参数"):
        params.validate_extra_params("TraceMoe", {key: "anything"})


def test_reserved_file_cannot_override_the_search_input():
    """核心安全断言：file 不能经由 extra params 传入。"""
    with pytest.raises(params.ParamError) as exc:
        params.validate_extra_params("TraceMoe", {"file": "/etc/passwd"})
    assert exc.value.key == "file"
    assert "保留参数" in str(exc.value)


def test_unknown_key_is_rejected_not_swallowed():
    """上游 search 都有 **kwargs；我们不能跟着静默吞掉。"""
    with pytest.raises(params.ParamError, match="不认识参数"):
        params.validate_extra_params("Yandex", {"totally_made_up": 1})


def test_old_wrong_name_cutBorders_is_rejected_with_a_hint():
    """旧公示名必须被拒 —— 而且要能提示正确名字。"""
    with pytest.raises(params.ParamError) as exc:
        params.validate_extra_params("TraceMoe", {"cutBorders": False})
    assert "cut_borders" in str(exc.value)


def test_valid_params_split_into_init_and_search():
    init, search = params.validate_extra_params(
        "SauceNAO", {"numres": 10, "hide": 2}
    )
    assert init == {"numres": 10, "hide": 2}
    assert search == {}

    init, search = params.validate_extra_params(
        "TraceMoe", {"cut_borders": False, "anilist_id": 12345}
    )
    assert init == {}
    assert search == {"cut_borders": False, "anilist_id": 12345}


# --------------------------------------------------------------------------
# 类型判定
# --------------------------------------------------------------------------

def test_bool_is_not_accepted_where_int_is_expected():
    """Python 里 isinstance(True, int) 为真 —— 这里必须显式判否。"""
    with pytest.raises(params.ParamError, match="需要整数"):
        params.validate_extra_params("SauceNAO", {"numres": True})


def test_int_is_not_accepted_where_bool_is_expected():
    with pytest.raises(params.ParamError, match="需要布尔值"):
        params.validate_extra_params("EHentai", {"is_ex": 1})


def test_string_is_not_accepted_where_bool_is_expected():
    """JSON 里写 "false" 是常见误用，不能当成 False。"""
    with pytest.raises(params.ParamError, match="需要布尔值"):
        params.validate_extra_params("EHentai", {"is_ex": "false"})


@pytest.mark.parametrize("value", [-1, 0, 41, 999])
def test_range_limits_are_enforced(value: int):
    with pytest.raises(params.ParamError):
        params.validate_extra_params("SauceNAO", {"numres": value})


def test_range_boundaries_are_inclusive():
    init, _ = params.validate_extra_params("SauceNAO", {"numres": 1})
    assert init["numres"] == 1
    init, _ = params.validate_extra_params("SauceNAO", {"numres": 40})
    assert init["numres"] == 40


def test_unknown_engine_has_no_contract():
    with pytest.raises(params.ParamError, match="未知引擎"):
        params.validate_extra_params("NotAnEngine", {})


# --------------------------------------------------------------------------
# 「拒绝发生在读文件之前」的直接证据
# --------------------------------------------------------------------------

def test_reserved_file_is_rejected_without_touching_the_filesystem(monkeypatch):
    """拒绝路径本身不做任何 I/O。

    注意这条**只覆盖校验函数这一层**，"在读取之前拒绝"的端到端证据在
    ``test_extras_whitelist.py`` —— 那条会驱动真正的搜索入口，并拦截
    ``builtins.open`` 与出站 HTTP，证明拒绝发生在读文件与建网络之前。
    """
    opened: list = []
    real_open = open

    def _spy_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _spy_open)

    with pytest.raises(params.ParamError):
        params.validate_extra_params("TraceMoe", {"file": "/etc/passwd"})

    assert opened == [], f"校验阶段不应有任何文件访问，实际：{opened}"


# --------------------------------------------------------------------------
# 错误信息可操作性
# --------------------------------------------------------------------------

def test_error_message_lists_allowed_params():
    with pytest.raises(params.ParamError) as exc:
        params.validate_extra_params("EHentai", {"nope": 1})
    message = str(exc.value)
    for expected in ("is_ex", "covers", "similar", "exp"):
        assert expected in message, f"报错里应列出允许的参数，缺 {expected}"


def test_engines_without_params_say_so():
    with pytest.raises(params.ParamError) as exc:
        params.validate_extra_params("Yandex", {"anything": 1})
    assert "（无）" in str(exc.value)
