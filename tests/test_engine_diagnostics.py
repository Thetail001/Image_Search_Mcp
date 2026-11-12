"""引擎失败诊断：上游改版、响应缺字段、上游 HTTP 错误都要能被分别认出。

这组测试针对 2026-11-13 的事故：Yandex 改成前端渲染后服务只回一句
"内部错误（ParsingError）"，把真实原因藏了起来，导致故障被当成工具坏掉。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from image_search_mcp import server
from PicImageSearch.exceptions import ParsingError


# ---------------------------------------------------------------- 异常分类


def test_upstream_parsing_error_is_reported_as_upstream_change():
    """上游改版要如实说成上游变了，不能归成内部错误。"""
    outcome = server._outcome_for_exception(
        ParsingError(
            message="Failed to extract search results from 'data-state'",
            engine="yandex",
            details="This usually indicates a change in the page structure",
        ),
        "Yandex",
    )

    assert outcome.is_error is True
    assert "UpstreamParsingError" in outcome.text
    # 两种可能都要说：改版，或被临时限流。给错方向等于没诊断。
    assert "改版" in outcome.text or "前端渲染" in outcome.text
    assert "限流" in outcome.text
    # 关键：不能再说成"内部错误"，那会把排查方向带错。
    assert "内部错误" not in outcome.text
    # 也不能鼓励立刻重试——限流场景下立刻重试只会更糟。
    assert "别立刻重试" in outcome.text


@pytest.mark.parametrize("exc", [KeyError("query_hash"), IndexError("list index out of range")])
def test_missing_field_errors_are_classified_as_response_shape(exc):
    """上游响应缺字段时库直接抛 KeyError/IndexError，要给可判读的说明。"""
    outcome = server._outcome_for_exception(exc, "Tineye")

    assert outcome.is_error is True
    assert "缺少解析器需要的字段" in outcome.text
    assert type(exc).__name__ in outcome.text
    assert "内部错误" not in outcome.text


def test_unknown_exception_still_falls_back_to_internal_error():
    """反向验证：真·未知异常仍走内部错误分支，说明分类没有把兜底吃掉。"""
    outcome = server._outcome_for_exception(RuntimeError("boom"), "BaiDu")

    assert outcome.is_error is True
    assert "内部错误" in outcome.text
    assert "RuntimeError" in outcome.text


# ---------------------------------------------------------------- 取证留痕


def test_upstream_body_snippet_reads_origin_text():
    response = SimpleNamespace(origin=SimpleNamespace(text="Just a moment..."))

    assert server._upstream_body_snippet(response) == "Just a moment..."


def test_upstream_body_snippet_redacts_credentials_and_data_uri():
    body = (
        "denied for api_key=SECRET123&x=1 "
        "img=data:image/png;base64,AAAABBBBCCCC"
    )

    snippet = server._upstream_body_snippet(SimpleNamespace(origin=SimpleNamespace(text=body)))

    assert "SECRET123" not in snippet
    assert "api_key=<redacted>" in snippet
    assert "AAAABBBBCCCC" not in snippet


def test_upstream_body_snippet_handles_bytes_and_missing_origin():
    assert server._upstream_body_snippet(
        SimpleNamespace(origin=SimpleNamespace(text=b"blocked"))
    ) == "blocked"
    # 没有 origin 字段时不能抛，只能返回空串。
    assert server._upstream_body_snippet(SimpleNamespace()) == ""
    assert server._upstream_body_snippet(None) == ""


def test_upstream_http_error_is_logged_with_code_and_body(caplog):
    """上游 4xx 必须留下证据——以前只回一句"上游 HTTP 403"，日志里什么都没有。"""
    response = SimpleNamespace(
        status_code=403,
        origin=SimpleNamespace(text="<title>Just a moment...</title>"),
    )

    with caplog.at_level(logging.WARNING, logger="image_search_mcp"):
        message = server._upstream_error("SauceNAO", response)

    assert message == "上游 HTTP 403"
    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "403" in joined
    assert "Just a moment" in joined


# ---------------------------------------------------------------- 默认引擎


def test_default_engine_is_a_known_engine_and_not_marked_broken():
    """默认引擎必须真实存在，而且不能是已知不可用的那个。"""
    assert server.DEFAULT_ENGINE in server.ENGINES
    assert server.DEFAULT_ENGINE not in server.KNOWN_ENGINE_ISSUES


def test_known_engine_issues_only_name_real_engines():
    """问题表里的名字必须是真引擎，避免写错名字后永远匹配不上。"""
    assert set(server.KNOWN_ENGINE_ISSUES) <= set(server.ENGINES)


# ---------------------------------------------------------------- 引擎信息


async def test_engine_info_marks_broken_engines():
    """get_engine_info 要能直接告诉调用方哪家有问题。"""
    text = server.get_engine_info("Yandex")

    assert "已知问题" in text
    assert "限流" in text


async def test_engine_info_all_carries_availability_warning():
    text = server.get_engine_info("all")

    assert "Availability warning" in text
    assert "BaiDu" in text
