"""MCP 协议层的契约：工具声明与结果形状。

为什么单独一个文件：这里断言的不是"业务对不对"，而是**协议上对外承诺了什么** ——
工具标题与注解、输出 schema、以及失败到底是 ``isError`` 还是"一条看起来正常的结果"。

全部走真实的 ``fastmcp.Client``（in-memory transport），不手搓 ``CallToolResult``：
只有走真实那条路，才测得到 fastmcp 究竟把我们的返回值变成了什么。
"""

from __future__ import annotations

import base64
import httpx
import pytest
from fastmcp import Client
from jsonschema import Draft202012Validator

from image_search_mcp import server
from image_search_mcp.server import SEARCH_OUTPUT_SCHEMA

#: 最小合法 PNG 头 + 填充，够走到"发请求"那一步
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32
SOURCE = base64.b64encode(PNG).decode()

TRACEMOE_RESPONSE = {
    "frameCount": 1,
    "error": "",
    "result": [
        {
            "anilist": 1,
            "filename": "f.mkv",
            "episode": 1,
            "from": 0.0,
            "to": 1.0,
            "similarity": 0.9,
            "video": "v",
            "image": "i",
        }
    ],
}
NO_RESULTS = {"frameCount": 0, "error": "", "result": []}
ANILIST_RESPONSE = {
    "data": {
        "Media": {
            "idMal": 1,
            "isAdult": False,
            "format": "TV",
            "type": "ANIME",
            "title": {"native": "N", "romaji": "R", "english": "E"},
            "synonyms": [],
            "startDate": {},
            "endDate": {},
            "coverImage": {"large": "x"},
        }
    }
}


@pytest.fixture
def trace_moe(mock_http):
    """记录出站请求，并给出可用的 TraceMoe 响应（可被用例改写）。"""
    seen: list[httpx.Request] = []
    state = {"payload": TRACEMOE_RESPONSE}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "anilist" in str(request.url):
            return httpx.Response(200, json=ANILIST_RESPONSE)
        return httpx.Response(200, json=state["payload"])

    mock_http(handler)
    return seen, state


def _pick(tools, name):
    matches = [t for t in tools if t.name == name]
    assert matches, f"没有名为 {name} 的工具"
    return matches[0]


def _text_of(result) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


# ==========================================================================
# 工具声明
# ==========================================================================

async def test_search_tool_declares_read_only_and_open_world():
    """搜索只读但会访问外部服务。

    这两个 hint 不声明的话，客户端只能一律把它当危险操作处理 —— 每次调用都弹确认，
    或者干脆不给出站类工具。注解是给客户端省事用的，不是装饰。
    """
    async with Client(server.mcp) as client:
        tool = _pick(await client.list_tools(), "search_image")

    assert tool.title, "工具应当有给人看的标题"
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.open_world_hint is True


async def test_engine_info_tool_is_read_only_but_not_open_world():
    """查引擎信息只读本地静态数据，不该被当成对外访问。"""
    async with Client(server.mcp) as client:
        tool = _pick(await client.list_tools(), "get_engine_info")

    assert tool.title
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.open_world_hint is False


async def test_search_tool_declares_the_output_schema_we_promised():
    async with Client(server.mcp) as client:
        tool = _pick(await client.list_tools(), "search_image")

    assert tool.output_schema is not None
    assert tool.output_schema["type"] == "object"
    assert tool.output_schema["properties"]["results"]["type"] == "array"
    assert set(tool.output_schema["required"]) == {
        "engine", "result_count", "returned", "truncated", "results",
    }


# ==========================================================================
# outputSchema 自己也得能失败（否则它只是个装饰）
# ==========================================================================

_VALID_PAYLOAD = {
    "engine": "TraceMoe",
    "result_count": 1,
    "returned": 1,
    "truncated": False,
    "results": [
        {"title": "t", "url": "u", "thumbnail": None, "similarity": 0.9}
    ],
}


def test_output_schema_accepts_the_shape_we_actually_produce():
    Draft202012Validator(SEARCH_OUTPUT_SCHEMA).validate(_VALID_PAYLOAD)


@pytest.mark.parametrize("broken", [
    {k: v for k, v in _VALID_PAYLOAD.items() if k != "engine"},          # 缺 required
    {**_VALID_PAYLOAD, "results": [{"title": "t"}]},                     # item 缺字段
    {**_VALID_PAYLOAD, "results": [{"title": "t", "url": "u", "thumbnail": None,
                                   "similarity": 0.9, "extra": 1}]},     # item 多字段
    {**_VALID_PAYLOAD, "result_count": "1"},                             # 类型不对
])
def test_output_schema_rejects_broken_payloads(broken):
    """没有这条，schema 写错也永远是"绿的"。"""
    with pytest.raises(Exception):
        Draft202012Validator(SEARCH_OUTPUT_SCHEMA).validate(broken)


# ==========================================================================
# 结果形状：isError 与 structuredContent
# ==========================================================================

async def test_successful_search_returns_structured_content_and_keeps_the_text(trace_moe):
    async with Client(server.mcp) as client:
        result = await client.call_tool_mcp(
            "search_image", {"source": SOURCE, "engine": "TraceMoe", "limit": 1}
        )

    assert result.is_error is False

    structured = result.structured_content
    assert structured is not None, "声明了 outputSchema 就必须给 structuredContent"
    Draft202012Validator(SEARCH_OUTPUT_SCHEMA).validate(structured)
    assert structured["engine"] == "TraceMoe"
    assert structured["result_count"] == 1
    assert structured["returned"] == 1
    assert structured["results"][0]["similarity"] == 90.0, (
        "上游把 0.9 归一成了百分数 90.0 —— 所以 schema 里这个字段允许 number 与 string 两种"
    )

    # 协议升级是加法：只看文本的客户端行为不变
    assert _text_of(result).startswith("Search Engine: TraceMoe")


@pytest.mark.parametrize(("arguments", "expected"), [
    ({"source": SOURCE, "engine": "NotAnEngine"}, "不支持的引擎"),
    ({"source": "", "engine": "TraceMoe"}, "source 为空"),
    ({"source": SOURCE, "engine": "TraceMoe", "limit": 0}, "limit"),
    ({"source": SOURCE, "engine": "TraceMoe", "extra_params_json": "{not json"}, "不是合法 JSON"),
])
async def test_input_rejections_come_back_as_is_error(trace_moe, arguments: dict, expected: str):
    """输入类失败必须是 ``isError: true``。

    旧写法把它当**正常结果**返回（一条以 ``Error: `` 开头的文本），客户端和模型
    无法与真正的结果区分 —— 复核报告 A 点过这条。
    """
    async with Client(server.mcp) as client:
        result = await client.call_tool_mcp("search_image", arguments)

    assert result.is_error is True, "输入被拒必须是 isError，而不是一条正常结果"
    assert expected in _text_of(result)


async def test_error_text_does_not_gain_a_tool_name_prefix(trace_moe):
    """用 ``ToolError`` 而不是让裸异常冒出去。

    例外：裸异常的消息会被 fastmcp 加上 ``Error calling tool 'x': `` 前缀（实测），
    那对模型只是噪音，也让报错文本不再是我们写的那句。
    """
    async with Client(server.mcp) as client:
        result = await client.call_tool_mcp(
            "search_image", {"source": SOURCE, "engine": "NotAnEngine"}
        )

    text = _text_of(result)
    assert "Error calling tool" not in text
    assert text.startswith("Error: "), "文本前缀保持不变，纯文本客户端不受影响"


async def test_no_results_is_not_an_error(trace_moe):
    """「没搜到」是**成功**的搜索。标成 error 会让模型去修一个不存在的问题。"""
    _, state = trace_moe
    state["payload"] = NO_RESULTS

    async with Client(server.mcp) as client:
        result = await client.call_tool_mcp(
            "search_image", {"source": SOURCE, "engine": "TraceMoe", "limit": 5}
        )

    assert result.is_error is False
    assert result.structured_content["result_count"] == 0
    assert result.structured_content["results"] == []
    assert result.structured_content["truncated"] is False


async def test_truncation_is_visible_in_the_structured_result(trace_moe):
    """截断了就要能从结构里看出来，不该只体现在一句文本里。"""
    _, state = trace_moe
    state["payload"] = {
        "frameCount": 3,
        "error": "",
        "result": [
            {"anilist": i, "filename": f"{i}.mkv", "episode": i, "from": 0.0,
             "to": 1.0, "similarity": 0.5, "video": "v", "image": "i"}
            for i in (1, 2, 3)
        ],
    }

    async with Client(server.mcp) as client:
        result = await client.call_tool_mcp(
            "search_image", {"source": SOURCE, "engine": "TraceMoe", "limit": 2}
        )

    assert result.is_error is False
    assert result.structured_content["result_count"] == 3
    assert result.structured_content["returned"] == 2
    assert result.structured_content["truncated"] is True
    assert len(result.structured_content["results"]) == 2


async def test_unknown_engine_in_engine_info_is_a_tool_error():
    """查一个不存在的引擎名也是工具执行错误，而不是一条正常结果。"""
    async with Client(server.mcp) as client:
        bad = await client.call_tool_mcp("get_engine_info", {"engine_name": "Nope"})
        good = await client.call_tool_mcp("get_engine_info", {"engine_name": "Yandex"})

    assert bad.is_error is True
    assert "not found" in _text_of(bad)
    assert good.is_error is False
    assert "Yandex" in _text_of(good)


async def test_unknown_tool_is_reported_by_the_framework_as_is_error():
    """**已知偏差，记录在案**。

    规范把"未知工具"归为**协议错误**（JSON-RPC error，如 -32602），
    但 fastmcp 4.0.10 把它变成了 ``isError: true`` 的工具执行错误（实测，
    消息为 ``Unknown tool: 'x'``）。

    这里断言**实测行为**而不是规范行为：写一条按规范断言、而我们并不实现的测试，
    只会得到一条永远在撒谎的测试。偏差记在这里 —— 哪天 fastmcp 改了，这条会红，
    那时再决定要不要跟着改。

    实用影响很小：消息本身可操作，模型照样能自我修正。
    （``client.call_tool`` 那条路上它会抛 ``ToolError``，是同一个事实的另一种表现。）
    """
    async with Client(server.mcp) as client:
        result = await client.call_tool_mcp("definitely_not_a_tool", {})

    assert result.is_error is True
    assert "Unknown tool" in _text_of(result)
