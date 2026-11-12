"""看门狗自身的测试：判定逻辑必须离线可验，且不能把"空结果"当通过。"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("engine_health", ROOT / "tools" / "engine_health.py")
engine_health = importlib.util.module_from_spec(_spec)
sys.modules["engine_health"] = engine_health
_spec.loader.exec_module(engine_health)

R = engine_health.EngineResult
OK, EMPTY = engine_health.STATUS_OK, engine_health.STATUS_EMPTY


def test_verdict_passes_when_required_engine_returns_results():
    passed, problems = engine_health.verdict(
        [R("BaiDu", OK, 8, 8.0), R("Yandex", "ERROR", 0, 1.0, "UpstreamParsingError")],
        ["BaiDu"],
    )

    assert passed is True
    assert problems == []


def test_verdict_fails_when_required_engine_is_empty():
    """空结果不算通过——这张图标都没收录和上游坏了必须区分，不能都放过。"""
    passed, problems = engine_health.verdict([R("BaiDu", EMPTY, 0, 8.0)], ["BaiDu"])

    assert passed is False
    assert "BaiDu" in problems[0]


def test_verdict_fails_when_required_engine_errored():
    passed, problems = engine_health.verdict(
        [R("BaiDu", "ERROR", 0, 1.0, "UpstreamParsingError: data-state")], ["BaiDu"]
    )

    assert passed is False
    assert "UpstreamParsingError" in problems[0]


def test_verdict_fails_when_required_engine_never_ran():
    passed, problems = engine_health.verdict([], ["BaiDu"])

    assert passed is False
    assert "未执行" in problems[0]


async def test_probe_maps_empty_response_to_empty_status():
    async def call(engine, url):
        return []

    result = await engine_health.probe("BaiDu", "https://example.com/x.jpg", call=call)

    assert result.status == EMPTY
    assert result.count == 0


async def test_probe_records_exception_type_without_raising():
    async def call(engine, url):
        raise ValueError("BCID not found on page")

    result = await engine_health.probe("Bing", "https://example.com/x.jpg", call=call)

    assert result.status == "ERROR"
    assert "ValueError" in result.detail
    assert "BCID" in result.detail


async def test_run_without_gap_checks_every_engine_and_keeps_order():
    async def call(engine, url):
        return [object()]

    results = await engine_health.run(
        ["BaiDu", "Tineye"], "https://example.com/x.jpg", gap=0, call=call
    )

    assert [r.engine for r in results] == ["BaiDu", "Tineye"]
    assert all(r.status == OK for r in results)


async def test_run_marks_unknown_engine_instead_of_crashing():
    results = await engine_health.run(["Nope"], "https://example.com/x.jpg", gap=0)

    assert results[0].status == "ERROR"
    assert "未知引擎" in results[0].detail


def test_main_returns_nonzero_when_required_engine_is_broken(monkeypatch, capsys):
    async def fake_run(engines, image_url, gap):
        return [R("BaiDu", "ERROR", 0, 1.0, "UpstreamParsingError")]

    monkeypatch.setattr(engine_health, "run", fake_run)
    code = engine_health.main(["--engines", "BaiDu", "--required", "BaiDu", "--gap", "0"])

    assert code == 1
    assert "失守" in capsys.readouterr().out


def test_main_returns_zero_and_json_when_healthy(monkeypatch, capsys):
    async def fake_run(engines, image_url, gap):
        return [R("BaiDu", OK, 8, 8.0)]

    monkeypatch.setattr(engine_health, "run", fake_run)
    code = engine_health.main(
        ["--engines", "BaiDu", "--required", "BaiDu", "--gap", "0", "--json"]
    )
    out = capsys.readouterr().out

    assert code == 0
    assert '"passed": true' in out.replace("True", "true")
    assert "BaiDu" in out
