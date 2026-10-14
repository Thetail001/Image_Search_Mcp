"""README 里的示例应能能通过参数契约。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from image_search_mcp import params, server

README = Path(__file__).resolve().parents[1] / "README.md"

#: ```json ... ``` 代码块
_FENCED_JSON = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)


def _readme_json_blocks() -> list[tuple[int, object]]:
    """返回 README 里每个 json 代码块的 (行号, 解析结果)。"""
    text = README.read_text(encoding="utf-8")
    blocks = []
    for match in _FENCED_JSON.finditer(text):
        line_number = text[: match.start()].count("\n") + 1
        raw = match.group(1)
        try:
            blocks.append((line_number, json.loads(raw)))
        except json.JSONDecodeError as exc:
            pytest.fail(f"README 第 {line_number} 行的 json 代码块解析失败：{exc}")
    return blocks


def _search_examples() -> list[tuple[int, dict]]:
    """挑出看起来像 ``search_image`` 调用的例子。"""
    examples = []
    for line_number, parsed in _readme_json_blocks():
        if not isinstance(parsed, dict):
            continue
        if "engine" not in parsed or "source" not in parsed:
            continue
        examples.append((line_number, parsed))
    return examples


def test_readme_has_at_least_one_search_example():
    """正对照：如果抽取逻辑失效（正则变了、README 结构变了），这里会先红。

    没有这条，下面那些断言会在"一个例子都没抽到"的情况下全部通过。
    """
    assert _search_examples(), "README 里应当至少有一个 search_image 调用示例"


def test_readme_examples_use_known_engines():
    for line_number, example in _search_examples():
        assert example["engine"] in server.ENGINES, (
            f"README 第 {line_number} 行的引擎 {example['engine']!r} 不存在"
        )


def test_readme_examples_have_valid_extra_params():
    """示例里的每个参数都必须在该引擎的契约里（用真实校验函数跑）。"""
    for line_number, example in _search_examples():
        raw = example.get("extra_params_json")
        if raw is None:
            continue
        assert isinstance(raw, str), (
            f"README 第 {line_number} 行：extra_params_json 应当是字符串"
            "（MCP 工具签名如此），不要在文档里写成嵌套对象"
        )
        try:
            params.validate_extra_params(example["engine"], json.loads(raw))
        except params.ParamError as exc:
            pytest.fail(
                f"README 第 {line_number} 行的示例参数通不过契约：{exc}\n"
                "文档里的参数名必须与上游签名一致，否则读者照着抄会拿到报错"
                "（或者更糟：静默无效）。"
            )


def test_readme_examples_have_valid_limit():
    for line_number, example in _search_examples():
        if "limit" not in example:
            continue
        limit = example["limit"]
        assert isinstance(limit, int) and not isinstance(limit, bool), (
            f"README 第 {line_number} 行：limit 应当是整数"
        )
        assert server.LIMIT_MIN <= limit <= server.LIMIT_MAX, (
            f"README 第 {line_number} 行：limit={limit} 超出 "
            f"{server.LIMIT_MIN}-{server.LIMIT_MAX}"
        )


def test_readme_does_not_document_the_global_cookie_variable():
    """新配置示例使用引擎专属变量；旧配置的兼容说明可保留在正文中。"""
    text = README.read_text(encoding="utf-8")
    offenders = [
        line for line in text.splitlines()
        if re.search(r"(export\s+|env\.|\$env:|\")\s*IMAGE_SEARCH_COOKIES\"?\s*[:=]", line)
        and "IMAGE_SEARCH_COOKIES_" not in line
    ]
    assert not offenders, (
        "README 里仍有配置示例教用户设置已废弃的 IMAGE_SEARCH_COOKIES：\n"
        + "\n".join(offenders)
    )
