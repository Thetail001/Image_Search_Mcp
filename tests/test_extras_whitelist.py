"""``extra_params`` 白名单的端到端回归（服务端任意文件读取）。

== 被测的那个缺陷 ==

旧代码的顺序是：

    search_kwargs["file"] = image_bytes          # 先装好输入
    search_kwargs.update(extra_params)           # 再让调用方覆盖

于是 ``{"file": "/etc/passwd"}`` 能把输入换成一个本地路径，
而上游 ``utils.read_file()`` 接受路径字符串并直接 ``open()``。
读出来的内容会跟着 multipart 请求**上传到搜索引擎站点** —— 一个完整的服务端文件外泄。

本文件里有一条 **正对照**（``test_control_detector_catches_the_original_bug``）：
它显式把校验换回旧行为，然后断言 canary 内容真的出现在出站请求体里。
没有这条，下面那些"拒绝"的测试可能只是碰巧为真。
"""

from __future__ import annotations

import base64

import httpx
import pytest

from image_search_mcp import params, server

#: 一个不可能真实存在的路径，用来当哨兵
CANARY_PATH = "/tmp/asa-canary-not-a-real-secret.txt"
CANARY_CONTENT = b"CANARY-CONTENT-7f3a91b2-should-never-leave-the-host"

#: ``_search_image_logic`` 现在返回 ``SearchOutcome``（文本 + 结构 + is_error）。
#: 存个别名让辅助函数用别名调用 —— 批量替换不该把它自己也算进去。
_search_logic = server._search_image_logic


async def _search_text(*args, **kwargs) -> str:
    """这个文件只关心文本与出站副作用，所以统一取文本那一半。"""
    outcome = await _search_logic(*args, **kwargs)
    return outcome.text


TRACEMOE_RESPONSE = {
    "frameCount": 1, "error": "", "result": [
        {"anilist": 1, "filename": "f.mkv", "episode": 1, "from": 0.0, "to": 1.0,
         "similarity": 0.9, "video": "v", "image": "i"}
    ],
}
ANILIST_RESPONSE = {"data": {"Media": {
    "idMal": 1, "isAdult": False, "format": "TV", "type": "ANIME",
    "title": {"native": "N", "romaji": "R", "english": "E"},
    "synonyms": [], "startDate": {}, "endDate": {}, "coverImage": {"large": "x"},
}}}


@pytest.fixture
def canary_file(tmp_path):
    """在临时目录放一个带哨兵内容的文件（不碰真实系统路径）。"""
    target = tmp_path / "canary.txt"
    target.write_bytes(CANARY_CONTENT)
    return str(target)


@pytest.fixture
def trace_moe_mock(mock_http):
    """记录所有出站请求（含请求体），并给出 TraceMoe + AniList 的合成响应。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "anilist" in str(request.url):
            return httpx.Response(200, json=ANILIST_RESPONSE)
        return httpx.Response(200, json=TRACEMOE_RESPONSE)

    mock_http(handler)
    return seen


def _uploaded_bodies(requests: list[httpx.Request]) -> bytes:
    return b"".join(bytes(r.content) for r in requests)


# ==========================================================================
# 正对照：证明这套探测真的能抓到"本地文件被读出并上传"
# ==========================================================================

async def test_control_upstream_read_file_would_exfiltrate_any_path(
    canary_file, trace_moe_mock
):
    """直接驱动上游引擎，把任意路径当 ``file`` 传进去 —— canary 内容会离开本机。

    这是**探测器自证**，也是漏洞的第二半：上游 ``utils.read_file()`` 对路径
    不做任何限制，读到的字节会跟着 multipart 请求发出去。
    如果这条不红，下面所有"被拒绝了"的断言都没有意义 ——
    它们可能只是因为我们压根没走到那条路。
    """
    from PicImageSearch import Network, TraceMoe

    async with Network() as net:
        client = TraceMoe(client=net)
        # 旧代码在 source 为 Base64 时走的正是这条：file 被换成一个路径字符串
        await client.search(file=canary_file)

    uploaded = _uploaded_bodies(trace_moe_mock)
    assert CANARY_CONTENT in uploaded, (
        "正对照失败：上游本应把任意路径读出来并上传。"
        "这条不红说明探测器无效，本文件其余断言都是假的。"
    )


async def test_input_is_applied_after_extras_so_it_cannot_be_overridden(
    canary_file, trace_moe_mock, monkeypatch
):
    """第二层防御：即使白名单被绕过，输入也不可能被 extras 覆盖。

    旧代码的顺序是"先装输入、再 update(extras)"，所以 extras 总能赢。
    现在改成"extras 先落位、输入后落位"，覆盖方向反过来了 ——
    于是这里把校验整个绕过，canary 依然读不到。
    两层各自独立，任何一层单独失效都不足以复现那个漏洞。
    """
    monkeypatch.setattr(
        params, "validate_extra_params",
        lambda engine, raw: ({}, dict(raw or {})),   # 旧行为：原样透传
    )
    source = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 32).decode()

    await _search_text(
        source=source, engine="TraceMoe",
        extra_params_json=f'{{"file": "{canary_file}"}}', limit=1,
    )

    assert CANARY_CONTENT not in _uploaded_bodies(trace_moe_mock), (
        "输入应用顺序错了：extras 里的 file 覆盖了真正的输入"
    )


# ==========================================================================
# 真实行为：保留键被拒，且拒绝发生在读文件与建网络之前
# ==========================================================================

async def test_reserved_file_key_never_reaches_the_filesystem(
    canary_file, trace_moe_mock, monkeypatch
):
    """带 ``file`` 的请求被拒，且**没有任何文件被打开**。

    注意"没有出站请求"**不足以**证明这一点（读完再拒也满足它），
    所以这里直接盯 ``builtins.open``。
    """
    opened: list[str] = []
    real_open = open

    def _spy(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _spy)
    source = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 32).decode()

    result = await _search_text(
        source=source, engine="TraceMoe",
        extra_params_json=f'{{"file": "{canary_file}"}}', limit=1,
    )

    assert "保留参数" in result
    assert opened == [], f"拒绝发生前有人打开了文件：{opened}"
    assert trace_moe_mock == [], "拒绝发生前发出了请求"


async def test_canary_content_never_leaves_the_host(canary_file, trace_moe_mock, monkeypatch):
    """与上面同一条路，直接断言哨兵内容没有出现在任何出站字节里。"""
    source = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 32).decode()
    await _search_text(
        source=source, engine="TraceMoe",
        extra_params_json=f'{{"file": "{canary_file}"}}', limit=1,
    )
    assert CANARY_CONTENT not in _uploaded_bodies(trace_moe_mock)


async def test_other_reserved_keys_are_rejected_too(trace_moe_mock):
    for key in ["url", "client", "api_key", "cookies", "proxies", "request_kwargs",
                "base_url"]:
        result = await _search_text(
            source="https://example.com/a.jpg", engine="TraceMoe",
            extra_params_json=f'{{"{key}": "x"}}', limit=1,
        )
        assert "保留参数" in result, f"{key} 应当被拒"
    assert trace_moe_mock == []


async def test_unknown_key_is_rejected_before_any_request(trace_moe_mock):
    result = await _search_text(
        source="https://example.com/a.jpg", engine="Yandex",
        extra_params_json='{"rpt": "imageview"}', limit=1,
    )
    assert "不认识参数" in result
    assert trace_moe_mock == []


@pytest.mark.parametrize("payload", ["[]", "1", '"str"', "[1,2]", "true"])
async def test_non_object_top_level_is_rejected(trace_moe_mock, payload: str):
    result = await _search_text(
        source="https://example.com/a.jpg", engine="Yandex",
        extra_params_json=payload, limit=1,
    )
    assert result.startswith("Error:")
    assert "对象" in result
    assert trace_moe_mock == []


@pytest.mark.parametrize("payload", ["null", "  null  "])
async def test_json_null_means_no_params_not_an_error(payload: str):
    """JSON 字面量 ``null`` 当成"没有参数"，不报错。

    客户端把可选字段序列化成 ``null`` 是常见做法。这是刻意的宽容，
    和上面拒掉数组/标量并不矛盾：那些是形态搞错了。
    """
    assert server._load_extra_params(payload) == {}


async def test_malformed_json_is_rejected(trace_moe_mock):
    result = await _search_text(
        source="https://example.com/a.jpg", engine="Yandex",
        extra_params_json="{not json", limit=1,
    )
    assert "不是合法 JSON" in result
    assert trace_moe_mock == []


# ==========================================================================
# 正例：合法参数要能走通（否则"全部拒绝"也能通过上面所有断言）
# ==========================================================================

async def test_valid_extra_params_still_work(trace_moe_mock):
    result = await _search_text(
        source="https://example.com/a.jpg", engine="TraceMoe",
        extra_params_json='{"cut_borders": false}', limit=1,
    )
    assert result.startswith("Search Engine: TraceMoe"), result
    urls = [str(r.url) for r in trace_moe_mock]
    assert any("api.trace.moe/search" in u for u in urls)
    # 参数真的生效了：cut_borders=false 时请求里不再带 cutBorders
    search_url = next(u for u in urls if "api.trace.moe/search" in u)
    assert "cutBorders" not in search_url


async def test_default_search_still_works(trace_moe_mock):
    """没有任何 extra params 的普通调用必须正常。"""
    result = await _search_text(
        source="https://example.com/a.jpg", engine="TraceMoe", limit=1,
    )
    assert result.startswith("Search Engine: TraceMoe")
    assert trace_moe_mock, "正常调用应当发出请求"


async def test_old_wrong_param_name_is_rejected_with_hint(trace_moe_mock):
    result = await _search_text(
        source="https://example.com/a.jpg", engine="TraceMoe",
        extra_params_json='{"cutBorders": false}', limit=1,
    )
    assert "cut_borders" in result, "报错应指出正确的参数名"
    assert trace_moe_mock == []
