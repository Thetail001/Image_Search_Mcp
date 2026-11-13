"""默认引擎失败后的兜底：只在没指定引擎时生效，只在报错时触发，结果要标明来源。

设计约束（每条都有对应测试）：
1. 显式指定引擎 → 不兜底，失败就报错（调用方点名要的，不能偷换）
2. 空结果是成功搜索 → 不兜底（否则同一次查询白做两遍）
3. 兜底成功 → 结构化结果必须带 fallback_from，文本必须说明结果来自谁
4. 全失败 → 错误里列出每一次失败原因，不能只留最后一条
5. 兜底链可配置；置空即关闭
"""
from __future__ import annotations

import asyncio

import pytest

from image_search_mcp import server


IMAGE_URL = "https://example.com/a.jpg"


def ok_outcome(engine: str, count: int = 2, limit: int = 5):
    from types import SimpleNamespace

    raw = [SimpleNamespace(title=f"{engine}-{i}", url=f"https://example.com/{i}")
           for i in range(count)]
    return server._format_response(engine, SimpleNamespace(raw=raw), limit)


def err_outcome(engine: str, reason: str = "上游页面结构变了"):
    return server.SearchOutcome(text=f"Error: {engine} {reason}", is_error=True)


class Recorder:
    """记录每次被调用的引擎，并按预设返回结果。"""

    def __init__(self, outcome_by_engine):
        self.outcome_by_engine = outcome_by_engine
        self.calls: list[str] = []

    async def __call__(self, source, name, extras, limit, **kwargs):
        self.calls.append(name)
        result = self.outcome_by_engine.get(name, ok_outcome(name))
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def _no_fallback_env(monkeypatch):
    monkeypatch.delenv(server.FALLBACK_ENV, raising=False)


# ---------------------------------------------------------------- 正常兜底


async def test_default_engine_error_falls_back_and_says_so(monkeypatch):
    monkeypatch.setenv(server.DEFAULT_ENGINE_ENV, "Yandex")
    recorder = Recorder({"Yandex": err_outcome("Yandex"), "BaiDu": ok_outcome("BaiDu")})
    monkeypatch.setattr(server, "_run_search", recorder)

    outcome = await server._search_image_logic(IMAGE_URL)

    assert recorder.calls == ["Yandex", "BaiDu"]
    assert outcome.is_error is False
    assert outcome.structured["engine"] == "BaiDu"
    assert outcome.structured["fallback_from"] == "Yandex"
    assert "兜底引擎 BaiDu" in outcome.text
    assert outcome.structured["result_count"] == 2


async def test_fallback_carries_original_failure_reason(monkeypatch):
    monkeypatch.setenv(server.DEFAULT_ENGINE_ENV, "Yandex")
    recorder = Recorder({"Yandex": err_outcome("Yandex", "被限流")})
    monkeypatch.setattr(server, "_run_search", recorder)

    outcome = await server._search_image_logic(IMAGE_URL)

    assert "被限流" in outcome.structured["fallback_reason"]
    assert "被限流" in outcome.text


# ---------------------------------------------------------------- 不该兜底的情况


async def test_explicit_engine_does_not_fall_back(monkeypatch):
    """点名要 Yandex 就只查 Yandex——偷换成别的引擎等于欺骗调用方。"""
    recorder = Recorder({"Yandex": err_outcome("Yandex"), "BaiDu": ok_outcome("BaiDu")})
    monkeypatch.setattr(server, "_run_search", recorder)

    outcome = await server._search_image_logic(IMAGE_URL, engine="Yandex")

    assert recorder.calls == ["Yandex"]
    assert outcome.is_error is True


async def test_empty_result_is_success_and_does_not_fall_back(monkeypatch):
    """空结果是一次成功搜索，不该触发兜底（否则同一查询白跑两遍）。"""
    monkeypatch.setenv(server.DEFAULT_ENGINE_ENV, "Yandex")
    empty = server._format_response("Yandex", type("R", (), {"raw": []})(), 5)
    recorder = Recorder({"Yandex": empty, "BaiDu": ok_outcome("BaiDu")})
    monkeypatch.setattr(server, "_run_search", recorder)

    outcome = await server._search_image_logic(IMAGE_URL)

    assert recorder.calls == ["Yandex"]
    assert outcome.is_error is False
    assert outcome.structured["result_count"] == 0


async def test_all_engine_mode_is_untouched_by_fallback(monkeypatch):
    monkeypatch.setenv(server.DEFAULT_ENGINE_ENV, "All")
    called = []

    async def fake_all(source, extras, limit, **kwargs):
        called.append("All")
        return server.SearchOutcome(text="Search Engine: All", is_error=False)

    monkeypatch.setattr(server, "_run_all_search", fake_all)
    outcome = await server._search_image_logic(IMAGE_URL)

    assert called == ["All"]
    assert outcome.is_error is False


async def test_empty_fallback_env_disables_fallback(monkeypatch):
    monkeypatch.setenv(server.DEFAULT_ENGINE_ENV, "Yandex")
    monkeypatch.setenv(server.FALLBACK_ENV, "")
    recorder = Recorder({"Yandex": err_outcome("Yandex")})
    monkeypatch.setattr(server, "_run_search", recorder)

    outcome = await server._search_image_logic(IMAGE_URL)

    assert recorder.calls == ["Yandex"]
    assert outcome.is_error is True


# ---------------------------------------------------------------- 全失败


async def test_all_engines_failing_reports_every_reason(monkeypatch):
    monkeypatch.setenv(server.DEFAULT_ENGINE_ENV, "Yandex")
    monkeypatch.setenv(server.FALLBACK_ENV, "BaiDu,Tineye")
    recorder = Recorder({
        "Yandex": err_outcome("Yandex", "空壳页"),
        "BaiDu": err_outcome("BaiDu", "取图失败"),
        "Tineye": err_outcome("Tineye", "JSON 解码失败"),
    })
    monkeypatch.setattr(server, "_run_search", recorder)

    outcome = await server._search_image_logic(IMAGE_URL)

    assert recorder.calls == ["Yandex", "BaiDu", "Tineye"]
    assert outcome.is_error is True
    for fragment in ("Yandex", "空壳页", "BaiDu", "取图失败", "Tineye", "JSON 解码失败"):
        assert fragment in outcome.text


async def test_fallback_engine_raising_does_not_stop_the_chain(monkeypatch):
    """第一个兜底引擎直接抛异常时，不能就此放弃后面的候选。"""
    monkeypatch.setenv(server.DEFAULT_ENGINE_ENV, "Yandex")
    monkeypatch.setenv(server.FALLBACK_ENV, "BaiDu,Tineye")
    recorder = Recorder({
        "Yandex": err_outcome("Yandex"),
        "BaiDu": RuntimeError("boom"),
        "Tineye": ok_outcome("Tineye"),
    })
    monkeypatch.setattr(server, "_run_search", recorder)

    outcome = await server._search_image_logic(IMAGE_URL)

    assert recorder.calls == ["Yandex", "BaiDu", "Tineye"]
    assert outcome.is_error is False
    assert outcome.structured["engine"] == "Tineye"


async def test_zero_remaining_budget_stops_without_trying(monkeypatch):
    """总期限已经用光时不该继续尝试兜底，并且要说明"未全部尝试"。"""
    monkeypatch.setenv(server.FALLBACK_ENV, "BaiDu")
    recorder = Recorder({"BaiDu": ok_outcome("BaiDu")})
    monkeypatch.setattr(server, "_run_search", recorder)
    # 直接把兜底阶段的期限设成负数：进入兜底时预算已耗尽。
    monkeypatch.setattr(server, "SEARCH_TOTAL_TIMEOUT", -1.0)

    outcome = await server._fallback_search(
        IMAGE_URL, "Yandex", err_outcome("Yandex"), None, 5,
    )

    assert recorder.calls == []          # 一个兜底引擎都不该被调用
    assert outcome.is_error is True
    assert "总期限已到" in outcome.text


# ---------------------------------------------------------------- 兜底链解析


@pytest.mark.parametrize("raw,expected", [
    (None, ["BaiDu"]),                                  # 未设置 → 默认链
    ("Tineye", ["Tineye"]),                             # 单个
    (" tracemoe , BaiDu ", ["TraceMoe", "BaiDu"]),      # 空白与大小写
    ("BaiDu,BaiDu", ["BaiDu"]),                         # 去重
    ("Nope,BaiDu", ["BaiDu"]),                          # 忽略未知
    ("", []),                                           # 置空 = 关闭
])
def test_fallback_chain_parsing(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(server.FALLBACK_ENV, raising=False)
    else:
        monkeypatch.setenv(server.FALLBACK_ENV, raw)

    assert server._fallback_chain("Yandex") == expected


def test_fallback_chain_never_contains_primary(monkeypatch):
    """主引擎自己不能出现在兜底链里，否则会把它再查一遍。"""
    monkeypatch.setenv(server.FALLBACK_ENV, "Yandex,BaiDu")

    assert server._fallback_chain("Yandex") == ["BaiDu"]
