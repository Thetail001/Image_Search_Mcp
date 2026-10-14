"""输入契约：主 search_image 只接受绝对 HTTP(S) URL。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastmcp import Client

from image_search_mcp import server

IMAGE_URL = "https://example.com/image.jpg"


def _success(name: str) -> server.SearchOutcome:
    return server.SearchOutcome(
        text=f"Search Engine: {name}\nNo results found.",
        structured=server._structured_results(name, [], 5),
    )


async def test_search_tool_schema_marks_source_as_http_url_only():
    async with Client(server.mcp) as client:
        tool = next(t for t in await client.list_tools() if t.name == "search_image")

    source = tool.input_schema["properties"]["source"]
    assert source["type"] == "string"
    assert "pattern" not in source, "schema 校验会在报错里回显整段 source，交给运行时拦截"
    assert "maxLength" not in source, "schema 校验会在报错里回显整段 source，交给运行时拦截"
    assert "Absolute HTTP(S) image URL" in source.get("description", "")
    assert "accepts URLs only" in (tool.description or "")
    assert "use a Base64 encoded string" not in (tool.description or "")
    assert "data:image/...;base64" not in (tool.description or "")


@pytest.mark.parametrize("source", [
    "test",                                  # 也可能是合法 base64；现在必须按 URL 校验
    "/tmp/a.png",
    "C:\\a.png",
    "file:///tmp/a.png",
    "data:image/png;base64,AAAA",
    "AAAA",
    "https://user:pass@example.com/a.jpg",
    "https://example.com/a.jpg#frag",
    "ftp://example.com/a.jpg",
    "https://example.com/a.jpg?x=1 y=2",
    "x" * (4096 + 1),
])
async def test_non_url_inputs_are_rejected_before_any_engine(monkeypatch, source):
    called = []

    async def run(*args, **kwargs):
        called.append((args, kwargs))
        return _success("Yandex")

    monkeypatch.setattr(server, "_run_search", run)
    result = await server._search_image_logic(source, "Yandex")
    assert result.is_error
    assert result.text.startswith("Error: [INPUT_URL_")
    assert not called


async def test_all_rejects_non_url_before_fanout(monkeypatch):
    called = []
    monkeypatch.setattr(server, "ENGINES", {"Yandex": object, "TraceMoe": object})

    async def run(*args, **kwargs):
        called.append((args, kwargs))
        return _success("Yandex")

    monkeypatch.setattr(server, "_run_search", run)
    result = await server._search_image_logic("AAAA", "All", json.dumps({}), 5)
    assert result.is_error
    assert result.text.startswith("Error: [INPUT_URL_")
    assert not called


async def test_valid_url_reaches_search(monkeypatch):
    seen = []

    async def run(source, name, extras, limit, **kwargs):
        seen.append((source, name, limit))
        return _success(name)

    monkeypatch.setattr(server, "_run_search", run)
    result = await server._search_image_logic(IMAGE_URL, "Yandex", None, 3)
    assert not result.is_error
    assert seen == [(IMAGE_URL, "Yandex", 3)]


async def test_engine_info_repeats_the_input_capabilities():
    async with Client(server.mcp) as client:
        all_info = await client.call_tool_mcp("get_engine_info", {"engine_name": "all"})
        one_info = await client.call_tool_mcp("get_engine_info", {"engine_name": "Yandex"})

    all_text = "".join(getattr(block, "text", "") for block in all_info.content)
    one_text = "".join(getattr(block, "text", "") for block in one_info.content)
    for text in (all_text, one_text):
        assert "absolute HTTP(S) image URLs only" in text
        assert "no upload or hosting endpoint" in text


async def test_mcp_error_for_inline_base64_is_safe_and_does_not_echo_input():
    payload = "A" * 100
    async with Client(server.mcp) as client:
        result = await client.call_tool_mcp(
            "search_image", {"source": payload, "engine": "Yandex", "limit": 1}
        )

    text = "".join(getattr(block, "text", "") for block in result.content)
    assert result.is_error is True
    assert text.startswith("Error: [INPUT_URL_REQUIRED]")
    assert payload not in text
