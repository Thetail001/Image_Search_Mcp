"""search_local_image 的注册、模式边界与输入上限。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from image_bytes import jpeg_bytes, png_bytes
from image_search_mcp import local_input, server

ENABLE_ENV = local_input.ENABLE_VAR
ROOTS_ENV = local_input.ROOTS_VAR


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "images"
    directory.mkdir()
    os.chmod(directory, 0o700)
    monkeypatch.setenv(ENABLE_ENV, "1")
    monkeypatch.setenv(ROOTS_ENV, str(directory))
    return directory


def _image(root: Path, payload: bytes | None = None) -> Path:
    target = root / "a.png"
    target.write_bytes(payload or png_bytes())
    os.chmod(target, 0o600)
    return target


def _success(name: str = "Yandex") -> server.SearchOutcome:
    return server.SearchOutcome(
        text=f"Search Engine: {name}\nNo results found.",
        structured=server._structured_results(name, [], 5),
    )


def _text_of(result) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


def _fresh_mcp() -> FastMCP:
    """每个测试用独立的 FastMCP，避免污染进程级的 server.mcp。"""
    return FastMCP("local-测试")


# --------------------------------------------------------------------------
# 注册边界：默认没有、启用后才有
# --------------------------------------------------------------------------

async def test_local_tool_is_not_registered_by_default():
    mcp = _fresh_mcp()
    assert server.register_local_tool(mcp) is False
    async with Client(mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert "search_local_image" not in names


async def test_local_tool_is_registered_when_enabled(root: Path):
    mcp = _fresh_mcp()
    assert server.register_local_tool(mcp) is True
    async with Client(mcp) as client:
        tool = next(t for t in await client.list_tools() if t.name == "search_local_image")

    assert tool.title
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.open_world_hint is True

    path = tool.input_schema["properties"]["path"]
    assert path["type"] == "string"
    assert "pattern" not in path, "schema 校验会在报错里回显整段 path，交给运行时拦截"
    assert "maxLength" not in path
    assert "absolute local" in path.get("description", "").lower()
    assert "stdio" in (tool.description or "").lower()


# --------------------------------------------------------------------------
# 调用路径
# --------------------------------------------------------------------------

async def test_local_tool_passes_file_bytes_to_the_engine(root: Path, monkeypatch):
    seen: list[tuple] = []

    async def fake_execute(engine, source, extra_params_json, limit, *, search_args=None):
        seen.append((engine, source, search_args, limit))
        return _success(engine)

    monkeypatch.setattr(server, "_execute_search", fake_execute)
    payload = jpeg_bytes()
    path = _image(root, payload)

    mcp = _fresh_mcp()
    server.register_local_tool(mcp)
    async with Client(mcp) as client:
        result = await client.call_tool_mcp(
            "search_local_image", {"path": str(path), "engine": "Yandex", "limit": 2}
        )

    assert result.is_error is False
    assert seen == [("Yandex", None, {"file": payload}, 2)], (
        "本机图片只读一次，交给引擎的必须是字节，不是路径"
    )


async def test_local_tool_reports_outside_path_without_echoing_it(root: Path, tmp_path: Path):
    outside = tmp_path / "secret-name.png"
    outside.write_bytes(png_bytes())

    mcp = _fresh_mcp()
    server.register_local_tool(mcp)
    async with Client(mcp) as client:
        result = await client.call_tool_mcp("search_local_image", {"path": str(outside)})

    text = _text_of(result)
    assert result.is_error is True
    assert "INPUT_PATH_DENIED" in text
    assert "secret-name" not in text


async def test_local_logic_refuses_when_disabled(root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ENABLE_ENV, raising=False)
    path = _image(root)
    outcome = await server._search_local_image_logic(str(path))
    assert outcome.is_error is True
    assert "LOCAL_INPUT_DISABLED" in outcome.text


async def test_local_logic_refuses_engines_without_file_support(root: Path, monkeypatch):
    path = _image(root)
    monkeypatch.setattr(server, "LOCAL_FILE_ENGINES", frozenset())
    outcome = await server._search_local_image_logic(str(path), "Yandex")
    assert outcome.is_error is True
    assert "ENGINE_INPUT_UNSUPPORTED" in outcome.text


async def test_local_all_only_calls_engines_that_accept_file_bytes(root: Path, monkeypatch):
    path = _image(root)
    payload = path.read_bytes()
    calls: list[tuple] = []

    async def fake_execute(engine, source, extra_params_json, limit, *, search_args=None):
        calls.append((engine, search_args))
        return _success(engine)

    monkeypatch.setattr(server, "ENGINES", {"A": object, "B": object})
    monkeypatch.setattr(server, "LOCAL_FILE_ENGINES", frozenset({"A", "B"}))
    monkeypatch.setattr(server, "_execute_search", fake_execute)

    outcome = await server._search_local_image_logic(str(path), "All", None, 2)
    assert outcome.is_error is False
    assert [name for name, _ in calls] == ["A", "B"]
    assert all(search_args == {"file": payload} for _, search_args in calls)
    assert "local" in outcome.text.lower()


async def test_local_all_skips_engines_outside_the_announced_set(root: Path, monkeypatch):
    path = _image(root)
    calls: list[str] = []

    async def fake_execute(engine, source, extra_params_json, limit, *, search_args=None):
        calls.append(engine)
        return _success(engine)

    monkeypatch.setattr(server, "ENGINES", {"A": object, "B": object})
    monkeypatch.setattr(server, "LOCAL_FILE_ENGINES", frozenset({"A"}))
    monkeypatch.setattr(server, "_execute_search", fake_execute)

    outcome = await server._search_local_image_logic(str(path), "All", None, 2)
    assert calls == ["A"]
    assert "B" in outcome.text, "被跳过的引擎也要在结果里说明，不能悄悄少一个"


# --------------------------------------------------------------------------
# 能力公告
# --------------------------------------------------------------------------

async def test_engine_info_hides_local_capability_by_default():
    async with Client(server.mcp) as client:
        result = await client.call_tool_mcp("get_engine_info", {"engine_name": "all"})
    text = _text_of(result)
    assert "search_local_image" not in text
    assert "absolute HTTP(S) image URLs only" in text


async def test_engine_info_advertises_local_capability_when_enabled(root: Path):
    async with Client(server.mcp) as client:
        result = await client.call_tool_mcp("get_engine_info", {"engine_name": "all"})
    text = _text_of(result)
    assert "search_local_image" in text
    assert "IMAGE_SEARCH_LOCAL_ROOTS" in text
    for engine in server.LOCAL_FILE_ENGINES:
        assert engine in text
    assert "file bytes" in text.lower()


def test_local_file_engines_match_the_upstream_signatures():
    """公告的每台引擎都必须真的有 file 参数 —— 手抄清单会腐烂。"""
    import inspect

    for name, cls in server.ENGINES.items():
        assert "file" in inspect.signature(cls.search).parameters, name
    assert server.LOCAL_FILE_ENGINES == frozenset(server.ENGINES)


# --------------------------------------------------------------------------
# 传输模式边界
# --------------------------------------------------------------------------

def test_sse_refuses_to_start_with_local_input_enabled(root: Path):
    from image_search_mcp import main as main_module

    with pytest.raises(SystemExit) as excinfo:
        main_module.main(["--sse"])
    assert "stdio" in str(excinfo.value)


def test_stdio_registers_local_tool_only_when_enabled(root: Path, monkeypatch):
    from image_search_mcp import main as main_module

    recorded: list[object] = []

    class StubMcp:
        def run(self) -> None:
            recorded.append("run")

    monkeypatch.setattr(main_module, "mcp", StubMcp())
    monkeypatch.setattr(main_module, "register_local_tool", lambda target: recorded.append(target))
    main_module.main([])
    assert len(recorded) == 2, "启用时必须先注册本机工具，再进入 stdio 循环"
    assert recorded[-1] == "run"


def test_stdio_without_local_env_does_not_register(monkeypatch):
    from image_search_mcp import main as main_module

    monkeypatch.delenv(ENABLE_ENV, raising=False)
    recorded: list[object] = []

    class StubMcp:
        def run(self) -> None:
            recorded.append("run")

    monkeypatch.setattr(main_module, "mcp", StubMcp())
    monkeypatch.setattr(main_module, "register_local_tool", lambda target: recorded.append(target))
    main_module.main([])
    assert recorded == ["run"]
