"""搜索流程：输入路由、凭据注入、limit 边界、错误出口。

用 TraceMoe 做主路径 —— 它是 JSON 接口，能造出干净的合成响应，
于是"请求真的发出去了"和"凭据真的带上了"这两件事都能直接观察。
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from PicImageSearch import Network

from image_search_mcp import server
from image_search_mcp.safe_download import SafeDownloader, DownloadPolicy

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
NO_RESULTS = {"frameCount": 0, "error": "", "result": []}


@pytest.fixture
def http_log(mock_http):
    """记录所有出站请求，并给出可解析的默认响应。"""
    seen: list[httpx.Request] = []
    state = {"search_payload": TRACEMOE_RESPONSE}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "anilist" in str(request.url):
            return httpx.Response(200, json=ANILIST_RESPONSE)
        return httpx.Response(200, json=state["search_payload"])

    mock_http(handler)
    return seen, state


def _png_b64(extra: bytes = b"") -> str:
    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 32 + extra).decode()


# ==========================================================================
# 凭据 / 代理：旧代码在"非空配置"下必炸
# ==========================================================================

async def test_non_empty_cookies_do_not_crash_and_are_sent_to_the_engine_domain(
    http_log, monkeypatch
):
    """旧代码的必炸路径：非空 cookie → ``'dict' object has no attribute 'split'``。

    现在不仅要"不炸"，还要断言 cookie 真的发给了该引擎的域。
    """
    monkeypatch.setenv("IMAGE_SEARCH_COOKIES_TRACEMOE", "sid=ENGINE_SECRET")
    seen, _ = http_log

    result = await server._search_image_logic(
        source="https://example.com/a.jpg", engine="TraceMoe", limit=1
    )

    assert "dict' object has no attribute" not in result
    engine_requests = [r for r in seen if "api.trace.moe" in str(r.url)]
    assert engine_requests, "应当已经向引擎发出请求"
    assert engine_requests[0].headers.get("cookie") == "sid=ENGINE_SECRET"


async def test_engine_cookies_do_not_go_to_the_anilist_endpoint(http_log, monkeypatch):
    """TraceMoe 会额外请求 trace.moe/anilist —— 同父域，凭据仍应带上；
    但**不能**发给完全无关的域。"""
    monkeypatch.setenv("IMAGE_SEARCH_COOKIES_TRACEMOE", "sid=ENGINE_SECRET")
    seen, _ = http_log
    await server._search_image_logic(
        source="https://example.com/a.jpg", engine="TraceMoe", limit=1
    )
    # 先确认真的发生了出站、且真的带了凭据：不加这两句的话，``seen`` 为空时
    # 循环体一次都不执行，这条测试会在"什么都没发生"的情况下通过（复核报告 B6）。
    assert seen, "应当已经有出站请求"
    assert any(r.headers.get("cookie") for r in seen), "至少一个请求要带上凭据，否则是空转"
    for request in seen:
        host = request.url.host
        assert host.endswith("trace.moe"), f"凭据不该发给 {host}"


async def test_global_cookie_without_owner_refuses_with_actionable_message(
    http_log, monkeypatch
):
    """归属不明的全局配置 → 明确拒绝并告诉怎么改，而不是"猜一个引擎发出去"。"""
    monkeypatch.setenv("IMAGE_SEARCH_COOKIES", "sid=X")
    seen, _ = http_log

    result = await server._search_image_logic(
        source="https://example.com/a.jpg", engine="TraceMoe", limit=1
    )

    assert "没有归属" in result
    assert "IMAGE_SEARCH_COOKIES_TRACEMOE" in result, "报错要指出具体该怎么改"
    assert seen == []


async def test_non_empty_proxy_does_not_crash(http_log, monkeypatch):
    """旧的 proxy 路径：``'dict' object has no attribute 'url'``（network.py:58）。"""
    monkeypatch.setenv("IMAGE_SEARCH_PROXY", "http://127.0.0.1:7890")
    seen, _ = http_log

    result = await server._search_image_logic(
        source="https://example.com/a.jpg", engine="TraceMoe", limit=1
    )

    assert "dict' object has no attribute" not in result


def test_network_kwargs_pass_strings_not_dicts():
    """直接盯构造参数的类型 —— 这就是旧 bug 的现场。"""
    import os
    os.environ["IMAGE_SEARCH_PROXY"] = "http://127.0.0.1:7890"
    try:
        kwargs, proxy = server._build_network_kwargs()
    finally:
        os.environ.pop("IMAGE_SEARCH_PROXY", None)

    assert isinstance(kwargs.get("proxies"), str)
    assert not isinstance(kwargs.get("proxies"), dict)
    assert "cookies" not in kwargs, "cookie 需要限域，不能在 Network 构造时传"
    assert proxy is not None


def test_network_accepts_the_string_forms_we_produce():
    """正对照：这些形态上游确实接受（否则"我们传对了"无从谈起）。"""
    Network(proxies="http://127.0.0.1:7890")
    Network(cookies="a=1; b=2")


# ==========================================================================
# 输入路由
# ==========================================================================

async def test_local_fetch_engine_downloads_through_our_downloader(monkeypatch):
    """EHentai / BaiDu 会用本地下载 —— 那条路必须换成我们的实现。"""
    resolved: list[str] = []

    async def _resolver(host, port):
        resolved.append(host)
        return ["93.184.216.34"]

    def _factory():
        return SafeDownloader(
            DownloadPolicy(),
            resolver=_resolver,
            transport_factory=lambda: httpx.MockTransport(
                lambda r: httpx.Response(200, content=b"DOWNLOADED-IMAGE-BYTES")
            ),
        )

    monkeypatch.setattr(server, "_make_downloader", _factory)

    result = await server._prepare_search_input("EHentai", "http://img.example.com/a.jpg")

    assert result == {"file": b"DOWNLOADED-IMAGE-BYTES"}
    assert resolved == ["img.example.com"], "源 URL 的域名必须经过我们的解析器"


async def test_remote_fetch_engine_passes_the_url_through():
    """其余引擎把 URL 交给搜索站点去抓 —— 那不是本机的出站面，不该被我们下载。"""
    for engine in ("Yandex", "TraceMoe", "SauceNAO", "Google"):
        result = await server._prepare_search_input(engine, "https://example.com/a.jpg")
        assert result == {"url": "https://example.com/a.jpg"}, engine


async def test_base64_source_becomes_bytes():
    result = await server._prepare_search_input("TraceMoe", _png_b64())
    assert isinstance(result["file"], bytes)
    assert result["file"].startswith(b"\x89PNG")


async def test_data_uri_is_accepted():
    result = await server._prepare_search_input(
        "TraceMoe", f"data:image/png;base64,{_png_b64()}"
    )
    assert result["file"].startswith(b"\x89PNG")


@pytest.mark.parametrize("bad", [
    "!!!!not base64!!!!",
    "",
    "   ",
    "data:image/png;utf8,hello",          # 不是 base64 data URI
    "data:image/png;base64",              # 缺逗号
    "data:image/png;base64,",             # 解码后为空
])
async def test_bad_base64_source_is_rejected(bad: str):
    with pytest.raises(server.SearchInputError):
        await server._prepare_search_input("TraceMoe", bad)


async def test_bad_base64_is_reported_through_the_public_entry(http_log):
    result = await server._search_image_logic(
        source="!!!!not base64!!!!", engine="TraceMoe", limit=1
    )
    assert result.startswith("Error:")
    assert "Base64" in result
    assert http_log[0] == [], "输入不合法时不该发出任何请求"


async def test_oversized_base64_is_rejected_before_decoding(http_log):
    huge = "A" * (server.MAX_IMAGE_BYTES // 3 * 4 + 1000)
    result = await server._search_image_logic(source=huge, engine="TraceMoe", limit=1)
    assert "上限" in result
    assert http_log[0] == []


# ==========================================================================
# limit
# ==========================================================================

@pytest.mark.parametrize("limit", [-1, 0, 51, 1000, -5])
async def test_out_of_range_limit_is_rejected_without_any_request(http_log, limit: int):
    """旧代码把 limit 直接用在切片上：``-1`` 产出
    "showing top -1" 却给 2 条；``0`` 仍然真的发了一次搜索请求。"""
    result = await server._search_image_logic(
        source="https://example.com/a.jpg", engine="TraceMoe", limit=limit
    )
    assert result.startswith("Error:")
    assert "limit" in result
    assert http_log[0] == [], f"limit={limit} 不该发出请求"


@pytest.mark.parametrize("limit", ["5", 5.0, True, None])
async def test_non_integer_limit_is_rejected(http_log, limit):
    result = await server._search_image_logic(
        source="https://example.com/a.jpg", engine="TraceMoe", limit=limit
    )
    assert result.startswith("Error:")
    assert http_log[0] == []


@pytest.mark.parametrize("limit", [server.LIMIT_MIN, server.LIMIT_MAX, 5])
async def test_valid_limit_is_accepted(http_log, limit: int):
    result = await server._search_image_logic(
        source="https://example.com/a.jpg", engine="TraceMoe", limit=limit
    )
    assert result.startswith("Search Engine: TraceMoe")


async def test_limit_actually_caps_the_output(http_log):
    """正例：limit 生效且措辞正确。"""
    seen, state = http_log
    state["search_payload"] = {
        "frameCount": 3, "error": "",
        "result": [
            {"anilist": i, "filename": f"f{i}.mkv", "episode": i, "from": 0.0,
             "to": 1.0, "similarity": 0.9, "video": "v", "image": "i"}
            for i in (1, 2, 3)
        ],
    }
    result = await server._search_image_logic(
        source="https://example.com/a.jpg", engine="TraceMoe", limit=2
    )
    assert "Found 3 results (showing top 2, truncated)" in result
    assert result.count("--- Result") == 2


async def test_no_results_is_not_an_error(http_log):
    """「没有结果」是正常状态，不是错误 —— 它不该被标成失败。"""
    seen, state = http_log
    state["search_payload"] = NO_RESULTS
    result = await server._search_image_logic(
        source="https://example.com/a.jpg", engine="TraceMoe", limit=5
    )
    assert result.startswith("Search Engine: TraceMoe")
    assert "No results found." in result


# ==========================================================================
# 错误出口
# ==========================================================================

async def test_unknown_engine_is_rejected(http_log):
    result = await server._search_image_logic(
        source="https://example.com/a.jpg", engine="DefinitelyNotAnEngine", limit=1
    )
    assert "不支持的引擎" in result
    assert http_log[0] == []


async def test_error_output_does_not_leak_a_traceback_or_server_paths(http_log):
    """旧代码把 ``traceback.format_exc()`` 拼进返回值 —— 含服务端绝对路径。

    这里制造一个上游解析失败（响应体不是预期结构），确认对外只有简短信息。
    """
    seen, state = http_log
    state["search_payload"] = {"unexpected": "shape"}

    result = await server._search_image_logic(
        source="https://example.com/a.jpg", engine="TraceMoe", limit=1
    )

    assert result.startswith("Error:") or result.startswith("Search Engine:")
    assert "Traceback" not in result
    assert "site-packages" not in result
    assert "/root/" not in result


async def test_unexpected_exception_is_reported_without_details(caplog, monkeypatch):
    """兜底分支：异常类型名留下、堆栈和细节只进日志。"""
    async def _boom(*args, **kwargs):
        raise RuntimeError("secret detail with /root/hermes-workspace/path")

    monkeypatch.setattr(server, "_prepare_search_input", _boom)

    result = await server._search_image_logic(
        source="https://example.com/a.jpg", engine="TraceMoe", limit=1
    )

    assert "RuntimeError" in result
    assert "/root/" not in result
    assert "Traceback" not in result
