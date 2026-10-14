"""Default engine selection and isolated, bounded multi-engine searches."""

import asyncio
import json
from types import SimpleNamespace

from fastmcp import Client
from jsonschema import Draft202012Validator
import httpx
import pytest

from image_search_mcp import server


NAMES = ("Yandex", "TraceMoe", "Ascii2D")


def success(name, count=1, limit=5):
    raw = [SimpleNamespace(title=f"{name}-{i}", url=f"https://example.com/{i}")
           for i in range(count)]
    return server._format_response(name, SimpleNamespace(raw=raw), limit)


@pytest.fixture
def selected_engines(monkeypatch):
    monkeypatch.setattr(server, "ENGINES", {name: object for name in NAMES})


@pytest.mark.parametrize("engine,configured,expected", [
    (None, "", "Yandex"),
    (None, " tracemoe ", "TraceMoe"),
    ("yandex", "Ascii2D", "Yandex"),
    ("Yandex", "Unknown", "Yandex"),
])
async def test_default_engine_and_explicit_override(monkeypatch, png_base64, engine, configured, expected):
    monkeypatch.setenv(server.DEFAULT_ENGINE_ENV, configured)

    async def run(source, name, extras, limit, **kwargs):
        return success(name)

    monkeypatch.setattr(server, "_run_search", run)
    result = await server._search_image_logic(png_base64, engine)
    assert result.structured["engine"] == expected


async def test_default_is_resolved_at_each_call(monkeypatch, png_base64):
    async def run(source, name, extras, limit, **kwargs):
        return success(name)

    monkeypatch.setattr(server, "_run_search", run)
    for name in ("Yandex", "Ascii2D"):
        monkeypatch.setenv(server.DEFAULT_ENGINE_ENV, name)
        result = await server._search_image_logic(png_base64)
        assert result.structured["engine"] == name


async def test_default_can_select_all(monkeypatch, selected_engines, png_base64):
    monkeypatch.setenv(server.DEFAULT_ENGINE_ENV, "All")

    async def run(source, name, extras, limit, **kwargs):
        return success(name)

    monkeypatch.setattr(server, "_run_search", run)
    result = await server._search_image_logic(png_base64)
    assert result.structured["engine"] == "All"
    assert {item["engine"] for item in result.structured["engine_outcomes"]} == set(NAMES)


async def test_invalid_default_is_rejected_before_search(monkeypatch, png_base64):
    monkeypatch.setenv(server.DEFAULT_ENGINE_ENV, "Unknown")
    result = await server._search_image_logic(png_base64)
    assert result.is_error
    assert "不支持的引擎" in result.text


@pytest.mark.parametrize("extra", [
    {"file": "/etc/passwd"}, {"cookies": "secret"}, {"cutBorders": True},
    {"bovw": "true"}, [], 1,
])
async def test_all_invalid_parameters_are_rejected_before_any_engine(
    monkeypatch, selected_engines, png_base64, extra,
):
    called = []

    async def run(*args, **kwargs):
        called.append(args)
        return success("Yandex")

    monkeypatch.setattr(server, "_run_search", run)
    result = await server._search_image_logic(png_base64, "All", json.dumps(extra))
    assert result.is_error
    assert not called


async def test_all_routes_parameters_and_decodes_base64_once(monkeypatch, selected_engines, png_base64):
    calls = {}
    decoded = []
    original = server._decode_image_source

    def decode(value):
        data = original(value)
        decoded.append(data)
        return data

    async def run(source, name, extras, limit, *, prepared_input):
        calls[name] = (json.loads(extras), prepared_input["file"])
        return success(name)

    monkeypatch.setattr(server, "_decode_image_source", decode)
    monkeypatch.setattr(server, "_run_search", run)
    result = await server._search_image_logic(
        png_base64, "all", '{"bovw": true, "cut_borders": false}',
    )
    assert not result.is_error
    assert len(decoded) == 1
    assert calls["Yandex"][0] == {}
    assert calls["Ascii2D"][0] == {"bovw": True}
    assert calls["TraceMoe"][0] == {"cut_borders": False}
    assert all(data is decoded[0] for _, data in calls.values())


async def test_all_keeps_partial_failures_and_no_results(monkeypatch, selected_engines, png_base64):
    async def run(source, name, extras, limit, **kwargs):
        if name == "TraceMoe":
            raise RuntimeError("private-secret")
        return success(name, count=0 if name == "Ascii2D" else 3, limit=limit)

    monkeypatch.setattr(server, "_run_search", run)
    async with Client(server.mcp) as client:
        result = await client.call_tool_mcp("search_image", {"source": png_base64, "engine": "All", "limit": 1})
    data = result.structured_content
    assert not result.is_error
    assert data["result_count"] == 3 and data["returned"] == 1 and data["truncated"]
    assert data["results"][0]["engine"] == "Yandex"
    assert [item["status"] for item in data["engine_outcomes"]] == ["success", "error", "no_results"]
    assert "private-secret" not in json.dumps(data)
    Draft202012Validator(server.SEARCH_OUTPUT_SCHEMA).validate(data)


async def test_all_failures_are_a_structured_tool_error(monkeypatch, selected_engines, png_base64):
    async def run(*args, **kwargs):
        raise RuntimeError("private-secret")

    monkeypatch.setattr(server, "_run_search", run)
    async with Client(server.mcp) as client:
        result = await client.call_tool_mcp("search_image", {"source": png_base64, "engine": "All"})
    assert result.is_error
    assert result.structured_content["all_failed"]
    assert len(result.structured_content["engine_outcomes"]) == len(NAMES)
    Draft202012Validator(server.SEARCH_OUTPUT_SCHEMA).validate(result.structured_content)


async def test_total_timeout_preserves_completed_results(monkeypatch, selected_engines, png_base64):
    monkeypatch.setattr(server, "SEARCH_TOTAL_TIMEOUT", 0.04)

    async def run(source, name, extras, limit, **kwargs):
        if name != "Yandex":
            await asyncio.sleep(1)
        return success(name)

    monkeypatch.setattr(server, "_run_search", run)
    result = await server._search_image_logic(png_base64, "All")
    assert not result.is_error
    assert result.structured["returned"] == 1
    assert result.structured["results"][0]["engine"] == "Yandex"
    assert [item["status"] for item in result.structured["engine_outcomes"]] == ["success", "error", "error"]


async def test_all_uses_separate_clients_and_scoped_credentials(monkeypatch, mock_http, png_base64):
    clients = []
    requests = []
    names = ("Yandex", "Ascii2D")
    hosts = ("yandex.com", "ascii2d.net")

    def engine(host):
        class Engine:
            def __init__(self, client, **kwargs):
                self.client = client
                clients.append(client)

            async def search(self, **kwargs):
                await self.client.get(f"https://{host}/search")
                return SimpleNamespace(raw=[])
        return Engine

    monkeypatch.setattr(server, "ENGINES", {name: engine(host) for name, host in zip(names, hosts)})
    for name in names:
        monkeypatch.setenv(f"IMAGE_SEARCH_COOKIES_{name.upper()}", f"sid={name}")

    def handler(request):
        requests.append(request)
        return httpx.Response(200)

    mock_http(handler)
    result = await server._search_image_logic(png_base64, "All")
    assert not result.is_error
    assert len({id(client) for client in clients}) == 2
    assert all(client.is_closed for client in clients)
    assert {request.url.host: request.headers.get("cookie") for request in requests} == {
        "yandex.com": "sid=Yandex", "ascii2d.net": "sid=Ascii2D",
    }


async def test_all_bounds_concurrency(monkeypatch, selected_engines, png_base64):
    active = maximum = 0
    monkeypatch.setattr(server, "ALL_CONCURRENCY", 2)

    async def run(source, name, extras, limit, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.02)
            return success(name)
        finally:
            active -= 1

    monkeypatch.setattr(server, "_run_search", run)
    result = await server._search_image_logic(png_base64, "All")
    assert not result.is_error
    assert maximum == 2 and active == 0


@pytest.mark.parametrize("total_timeout,engine_timeout", [(0.04, 1.0), (1.0, 0.04)])
async def test_all_timeouts_cancel_pending_work(
    monkeypatch, selected_engines, png_base64, total_timeout, engine_timeout,
):
    active = 0
    started = []
    monkeypatch.setattr(server, "SEARCH_TOTAL_TIMEOUT", total_timeout)
    monkeypatch.setattr(server, "ALL_ENGINE_TIMEOUT", engine_timeout)
    monkeypatch.setattr(server, "ALL_CONCURRENCY", 1)

    async def run(*args, **kwargs):
        nonlocal active
        active += 1
        started.append(True)
        try:
            await asyncio.sleep(1)
            return success("Yandex")
        finally:
            active -= 1

    monkeypatch.setattr(server, "_run_search", run)
    result = await server._search_image_logic(png_base64, "All")
    assert result.is_error and result.structured["all_failed"]
    assert all("超时" in item["error"] or "期限" in item["error"]
               for item in result.structured["engine_outcomes"])
    assert active == 0
    assert len(started) == (1 if total_timeout < engine_timeout else len(NAMES))


async def test_single_search_has_a_total_deadline(monkeypatch, png_base64):
    monkeypatch.setattr(server, "SEARCH_TOTAL_TIMEOUT", 0.04)

    async def run(*args, **kwargs):
        await asyncio.sleep(1)
        return success("Yandex")

    monkeypatch.setattr(server, "_run_search", run)
    result = await server._search_image_logic(png_base64, "Yandex")
    assert result.is_error and "期限" in result.text


async def test_all_local_downloads_still_reject_private_addresses(monkeypatch):
    class Engine:
        def __init__(self, client, **kwargs):
            pass

        async def search(self, **kwargs):
            return SimpleNamespace(raw=[])

    monkeypatch.setattr(server, "ENGINES", {name: Engine for name in ("Yandex", "EHentai", "BaiDu")})
    result = await server._search_image_logic("http://127.0.0.1/image.png", "All")
    statuses = {item["engine"]: item for item in result.structured["engine_outcomes"]}
    assert statuses["Yandex"]["status"] == "no_results"
    for name in ("EHentai", "BaiDu"):
        assert statuses[name]["status"] == "error"
        assert "blocked_address" in statuses[name]["error"]
