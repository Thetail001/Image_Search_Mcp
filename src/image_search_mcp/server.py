"""MCP 工具、图片输入处理与搜索结果格式化。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Annotated, Any, Optional
from urllib.parse import urlsplit

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations
from pydantic import Field, StrictInt

# FastMCP 是直接依赖，缺失时直接报告导入错误。

from PicImageSearch import (
    Ascii2D,
    BaiDu,
    Bing,
    EHentai,
    Google,
    GoogleLens,
    Iqdb,
    Network,
    SauceNAO,
    Tineye,
    TraceMoe,
    Yandex,
)

from . import local_input, params
from .local_input import LocalInputError
from .credentials import (
    CredentialError,
    ignored_proxy_vars,
    install_cookie_jar,
    load_engine_credentials,
    load_proxy,
    strip_inherited_proxies,
    PROXY_VAR,
)
from .safe_download import (
    ENGINES_THAT_FETCH_LOCALLY,
    DownloadError,
    SafeDownloader,
)

logger = logging.getLogger("image_search_mcp")

mcp = FastMCP("image-search")

#: 引擎名 → 上游实现类。键集必须与 ``params.CONTRACTS`` 一致（有测试把关）。
ENGINES: dict[str, type] = {
    "SauceNAO": SauceNAO,
    "Google": Google,
    "TraceMoe": TraceMoe,
    "Ascii2D": Ascii2D,
    "BaiDu": BaiDu,
    "Bing": Bing,
    "EHentai": EHentai,
    "GoogleLens": GoogleLens,
    "Iqdb": Iqdb,
    "Tineye": Tineye,
    "Yandex": Yandex,
}

#: 结果条数的合法范围。旧代码直接把它用在切片上，``-1`` 会产出
#: "Found 3 results (showing top -1)" 而实际给 2 条。
LIMIT_MIN = 1
LIMIT_MAX = 50
DEFAULT_LIMIT = 5
DEFAULT_ENGINE = "Yandex"
DEFAULT_ENGINE_ENV = "IMAGE_SEARCH_DEFAULT_ENGINE"
ALL_ENGINE = "All"
SEARCH_TOTAL_TIMEOUT = 60.0
ALL_ENGINE_TIMEOUT = 20.0
ALL_CONCURRENCY = 3

#: 主输入只接受 URL；4096 是产品预算，不是 URL 协议上限。
MAX_URL_CHARS = 4096

#: 本机 stdio 模式下允许直接吃 ``file: bytes`` 的引擎集合。
#: 必须由测试对着上游 ``search()`` 签名核对，不能手抄。
LOCAL_FILE_ENGINES: frozenset[str] = frozenset(ENGINES)


def _input_capabilities() -> str:
    """按当前运行模式生成输入能力说明（远程/本机两套契约）。"""
    text = (
        "Input capabilities: search_image accepts absolute HTTP(S) image URLs only "
        f"(at most {MAX_URL_CHARS} characters). Local paths, file URIs, data URIs, "
        "and inline Base64 are unsupported. This server provides no upload or hosting endpoint. "
        "BaiDu and EHentai URLs are fetched by this MCP server under its download policy; "
        "other engines receive the URL for upstream retrieval."
    )
    if local_input.enabled():
        text += (
            " Local stdio mode additionally provides search_local_image(path=...): the path must be an "
            f"absolute regular file under a server-configured allowlist ({local_input.ROOTS_VAR}), "
            "must not cross a symlink, and must be a JPEG, PNG, or static WebP of at most "
            f"{local_input.MAX_BYTES // (1024 * 1024)} MiB and "
            f"{local_input.MAX_PIXELS // 1_000_000} MP. "
            "Its bytes are read once and disclosed to the selected search providers. "
            "HTTP/SSE mode never registers local file input."
        )
    return text


class SearchInputError(ValueError):
    """调用方输入不合法。与"上游出错"区分开，便于调用方判断该不该重试。"""


@dataclass(frozen=True)
class SearchOutcome:
    """搜索的文本结果、结构化结果和错误状态。"""

    text: str
    structured: dict[str, Any] | None = None
    is_error: bool = False


#: 结构化输出声明通用字段；引擎专属字段保留在文本中。
SEARCH_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "engine": {"type": "string", "description": "本次实际使用的引擎"},
        "result_count": {"type": "integer", "description": "上游返回的结果总数"},
        "returned": {"type": "integer", "description": "本次实际返回的条数"},
        "truncated": {"type": "boolean", "description": "是否因为 limit 而截断"},
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": ["string", "null"]},
                    "url": {"type": ["string", "null"]},
                    "thumbnail": {"type": ["string", "null"]},
                    "similarity": {
                        "type": ["number", "string", "null"],
                        "description": "各引擎口径不一致：可能是数字，也可能是 '95%' 这类字符串",
                    },
                },
                "required": ["title", "url", "thumbnail", "similarity"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["engine", "result_count", "returned", "truncated", "results"],
}

# All 沿用通用字段，并增加各引擎状态；单引擎结果的形状保持不变。
RESULT_ITEM_SCHEMA = SEARCH_OUTPUT_SCHEMA["properties"]["results"]["items"]
RESULT_ITEM_SCHEMA["properties"]["engine"] = {"type": "string"}
SEARCH_OUTPUT_SCHEMA["properties"]["all_failed"] = {"type": "boolean"}
SEARCH_OUTPUT_SCHEMA["properties"]["engine_outcomes"] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "engine": {"type": "string"},
            "status": {"enum": ["success", "no_results", "error"]},
            "error": {"type": ["string", "null"]},
            "result_count": {"type": "integer"},
            "returned": {"type": "integer"},
            "truncated": {"type": "boolean"},
            "results": {"type": "array", "items": RESULT_ITEM_SCHEMA},
        },
        "required": ["engine", "status", "error", "result_count", "returned", "truncated", "results"],
        "additionalProperties": False,
    },
}
SEARCH_OUTPUT_SCHEMA["allOf"] = [{
    "if": {"properties": {"engine": {"const": ALL_ENGINE}}, "required": ["engine"]},
    "then": {"required": ["all_failed", "engine_outcomes"]},
}]

#: 搜索是只读且会访问外部服务；不声明这两点，客户端只能一律当危险操作处理。
SEARCH_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    idempotent_hint=True,
    open_world_hint=True,
)

#: 查引擎信息是纯本地的静态文档查询。
INFO_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _similarity_or_none(value: Any) -> float | str | None:
    """保留数值或字符串相似度，排除布尔值与复杂对象。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, str)):
        return value
    return None


def _structured_results(engine: str, raw: Any, limit: int) -> dict[str, Any]:
    shown = min(len(raw), limit)
    return {
        "engine": engine,
        "result_count": len(raw),
        "returned": shown,
        "truncated": len(raw) > shown,
        "results": [
            {
                "title": _text_or_none(getattr(item, "title", None)),
                "url": _text_or_none(getattr(item, "url", None)),
                "thumbnail": _text_or_none(getattr(item, "thumbnail", None)),
                "similarity": _similarity_or_none(getattr(item, "similarity", None)),
            }
            for item in raw[:shown]
        ],
    }


# --------------------------------------------------------------------------
# 工具：引擎信息（内容由参数契约生成，不手写副本）
# --------------------------------------------------------------------------

@mcp.tool(title="查询引擎信息", annotations=INFO_ANNOTATIONS)
def get_engine_info(engine_name: str = "all") -> str:
    """Get information about supported search engines.

    Args:
        engine_name: The name of the engine to get details for, or "all" for a summary list.
                     Default: "all".
    """
    if engine_name.lower() == "all":
        local_enabled = local_input.enabled()
        lines = ["Supported Search Engines:"]
        lines.append("- All: 查询全部引擎；limit 按每个引擎计算，单个引擎失败不影响其他结果。")
        for name in params.CONTRACTS:
            lines.append(f"- {name}: {params.engine_brief(name, local_enabled=local_enabled)}")
        if local_enabled:
            lines.append("")
            lines.append("Local file input support:")
            for name in params.CONTRACTS:
                capability = "URL and local file bytes" if name in LOCAL_FILE_ENGINES else "URL only"
                lines.append(f"- {name}: {capability}")
        lines.append("")
        lines.append(_input_capabilities())
        return "\n".join(lines)

    local_enabled = local_input.enabled()
    for name in params.CONTRACTS:
        if name.lower() == engine_name.lower():
            return (
                params.engine_details(name, local_enabled=local_enabled)
                + "\n\n"
                + _input_capabilities()
            )

    # 使用 ToolError 返回工具执行错误。
    raise ToolError(
        f"Engine '{engine_name}' not found. Supported: {', '.join(params.CONTRACTS)}"
    )


# --------------------------------------------------------------------------
# 结果格式化
# --------------------------------------------------------------------------

def _format_result_item(item: Any, engine: str) -> str:
    """格式化单条结果，保留集数和时间点等字段的零值。"""
    lines: list[str] = []

    # 通用字段
    if getattr(item, "title", None):
        lines.append(f"Title: {item.title}")
    if getattr(item, "url", None):
        lines.append(f"URL: {item.url}")
    if getattr(item, "thumbnail", None):
        lines.append(f"Thumbnail: {item.thumbnail}")

    # 相似度：只要对象带这个字段就输出（不要再按引擎分叉）
    similarity = getattr(item, "similarity", None)
    if similarity is not None:
        lines.append(f"Similarity: {similarity}%")

    if engine == "SauceNAO":
        if getattr(item, "author", None):
            lines.append(f"Author: {item.author}")
        if getattr(item, "author_url", None):
            lines.append(f"Author URL: {item.author_url}")
        if getattr(item, "pixiv_id", None):
            lines.append(f"Pixiv ID: {item.pixiv_id}")
        if getattr(item, "member_id", None):
            lines.append(f"Member ID: {item.member_id}")

    elif engine == "TraceMoe":
        episode = getattr(item, "episode", None)
        if episode is not None:
            lines.append(f"Episode: {episode}")

        # TraceMoeItem 用的是首字母大写的 .From / .To
        start_time = getattr(item, "From", None)
        end_time = getattr(item, "To", None)
        if start_time is not None:
            end_display = "?" if end_time is None else f"{end_time}"
            lines.append(f"Time: {start_time}s - {end_display}s")

        for attr, label in (
            ("title_english", "English Title"),
            ("title_romaji", "Romaji Title"),
            ("title_native", "Native Title"),
            # 上游当前不会返回中文标题（GraphQL query 未请求该字段），
            # 这里照样读 —— 上游补上以后自动生效
            ("title_chinese", "Chinese Title"),
        ):
            value = getattr(item, attr, None)
            if value:
                lines.append(f"{label}: {value}")

        if getattr(item, "filename", None):
            lines.append(f"Filename: {item.filename}")
        if getattr(item, "video", None):
            lines.append(f"Video: {item.video}")
        if getattr(item, "image", None):
            lines.append(f"Preview: {item.image}")

    elif engine == "Ascii2D":
        if getattr(item, "author", None):
            lines.append(f"Author: {item.author}")
        if getattr(item, "author_url", None):
            lines.append(f"Author URL: {item.author_url}")
        if getattr(item, "source", None):
            lines.append(f"Source Type: {item.source}")

    elif engine == "EHentai":
        if getattr(item, "type", None):
            lines.append(f"Category: {item.type}")
        if getattr(item, "date", None):
            lines.append(f"Date: {item.date}")

    elif engine == "Yandex":
        if getattr(item, "source", None):
            lines.append(f"Source: {item.source}")
        if getattr(item, "content", None):
            lines.append(f"Content: {item.content}")
        if getattr(item, "size", None):
            lines.append(f"Size: {item.size}")

    if getattr(item, "ext_urls", None):
        lines.append(f"External URLs: {item.ext_urls}")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# 输入解析
# --------------------------------------------------------------------------

def _validate_image_url(source: str) -> str:
    """校验主输入：只接受绝对 HTTP(S) URL，返回原样字符串。"""
    if not isinstance(source, str) or not source.strip():
        raise SearchInputError(
            "[INPUT_URL_REQUIRED] search_image accepts absolute HTTP(S) image URLs only. "
            "Local paths, file URIs, data URIs, and inline Base64 are unsupported. "
            "This server provides no upload or hosting endpoint."
        )

    trimmed = source.strip()
    if len(trimmed) > MAX_URL_CHARS:
        raise SearchInputError(
            f"[INPUT_URL_TOO_LONG] source exceeds the {MAX_URL_CHARS}-character URL limit. "
            "A shorter authorized image URL is required; removing signature parameters can invalidate access."
        )

    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in trimmed):
        raise SearchInputError(
            "[INPUT_URL_INVALID] source is not a supported absolute HTTP(S) URL. "
            "A non-empty host and a valid port are required; whitespace, control characters, "
            "URL userinfo, and fragments are unsupported."
        )

    try:
        parts = urlsplit(trimmed)
        # 访问 .port 会让坏端口在这里炸出来，而不是留给下游。
        _ = parts.port
    except ValueError as exc:
        raise SearchInputError(
            "[INPUT_URL_INVALID] source is not a supported absolute HTTP(S) URL. "
            "A non-empty host and a valid port are required; whitespace, control characters, "
            "URL userinfo, and fragments are unsupported."
        ) from exc

    if parts.scheme.lower() not in {"http", "https"}:
        raise SearchInputError(
            "[INPUT_URL_REQUIRED] search_image accepts absolute HTTP(S) image URLs only. "
            "Local paths, file URIs, data URIs, and inline Base64 are unsupported. "
            "This server provides no upload or hosting endpoint."
        )

    if not parts.hostname or parts.username is not None or parts.password is not None or parts.fragment:
        raise SearchInputError(
            "[INPUT_URL_INVALID] source is not a supported absolute HTTP(S) URL. "
            "A non-empty host and a valid port are required; whitespace, control characters, "
            "URL userinfo, and fragments are unsupported."
        )

    return trimmed


def _resolve_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise SearchInputError(f"limit 需要整数，收到 {type(limit).__name__}")
    if not (LIMIT_MIN <= limit <= LIMIT_MAX):
        raise SearchInputError(f"limit 必须在 {LIMIT_MIN}-{LIMIT_MAX} 之间（收到 {limit}）")
    return limit


# --------------------------------------------------------------------------
# 核心逻辑
# --------------------------------------------------------------------------

def _load_extra_params(raw: Optional[str]) -> dict:
    """解析附加参数 JSON。省略、空字符串和 JSON null 均表示没有参数。"""
    if raw is None or raw == "":
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SearchInputError(f"extra_params_json 不是合法 JSON：{exc}") from exc
    if parsed is None:
        return {}
    return parsed


def _build_network_kwargs() -> tuple[dict, Any]:
    """组装 Network 参数。代理来自显式配置，Cookies 在 client 创建后安装。"""
    kwargs: dict[str, Any] = {}

    proxy = load_proxy()
    if proxy is not None:
        kwargs["proxies"] = proxy.url

    ignored = ignored_proxy_vars()
    if ignored and proxy is None:
        logger.warning(
            "检测到 %s，但本服务不再隐式使用它们；如需要请显式设置 IMAGE_SEARCH_PROXY",
            ", ".join(ignored),
        )

    return kwargs, proxy


def _make_downloader() -> SafeDownloader:
    """构造图片下载器；测试可替换此工厂以注入受控连接。"""
    return SafeDownloader()


async def _prepare_search_input(
    engine: str,
    source: str,
) -> dict[str, Any]:
    """准备图片输入。EHentai 和 BaiDu 的 URL 由安全下载器取回，其余引擎直接接收 URL。"""
    trimmed = _validate_image_url(source)

    if engine in ENGINES_THAT_FETCH_LOCALLY:
        downloader = _make_downloader()
        data = await downloader.fetch(trimmed)
        return {"file": data}

    return {"url": trimmed}


def _outcome_for_exception(exc: Exception, engine: Optional[str]) -> SearchOutcome:
    """把已知异常翻译成对外文本；未知异常只给类型名，细节进日志。"""
    if isinstance(exc, asyncio.TimeoutError):
        return SearchOutcome(text="Error: 搜索总期限已到", is_error=True)
    if isinstance(exc, (SearchInputError, LocalInputError)):
        return SearchOutcome(text=f"Error: {exc}", is_error=True)
    if isinstance(exc, params.ParamError):
        return SearchOutcome(text=f"Error: {exc}", is_error=True)
    if isinstance(exc, CredentialError):
        message = f"Error: {exc}"
        if exc.hint:
            message += f"\nHint: {exc.hint}"
        return SearchOutcome(text=message, is_error=True)
    if isinstance(exc, DownloadError):
        return SearchOutcome(
            text=f"Error: 取图失败 [{exc.reason}] {exc}", is_error=True
        )
    # 详细堆栈仅写入日志，客户端只接收异常类型。
    logger.exception("图片搜索失败 engine=%s", engine)
    return SearchOutcome(
        text=(
            f"Error: 搜索过程中发生内部错误（{type(exc).__name__}）。"
            "详细信息见服务端日志。"
        ),
        is_error=True,
    )


async def _search_image_logic(
    source: str,
    engine: Optional[str] = None,
    extra_params_json: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    *,
    _prepared_input: dict[str, Any] | None = None,
) -> SearchOutcome:
    """执行 URL 搜索，将输入、配置和上游异常整理成 SearchOutcome。"""
    try:
        # 输入校验必须早于引擎解析与 All 分发：否则 mock/分发层会绕过它。
        _validate_image_url(source)
        resolved_engine = _resolve_engine(engine)
        if resolved_engine == ALL_ENGINE:
            return await _run_all_search(source, extra_params_json, limit)
        return await asyncio.wait_for(
            _run_search(source, resolved_engine, extra_params_json, limit, prepared_input=_prepared_input),
            SEARCH_TOTAL_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - 统一交给 _outcome_for_exception 分流
        return _outcome_for_exception(exc, engine)


async def _search_local_image_logic(
    path: str,
    engine: Optional[str] = None,
    extra_params_json: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
) -> SearchOutcome:
    """执行本机图片搜索：只读一次字节，再把字节交给引擎。"""
    try:
        if not local_input.enabled():
            raise LocalInputError(
                "LOCAL_INPUT_DISABLED",
                "本机图片输入未启用：这只在本机 stdio 模式且配置 allowlist 后可用。",
            )
        data = local_input.read_image(path)
        resolved_engine = _resolve_engine(engine)
        search_args = {"file": data}
        if resolved_engine == ALL_ENGINE:
            return await _run_all_search(None, extra_params_json, limit, prepared_input=search_args)
        if resolved_engine not in LOCAL_FILE_ENGINES:
            raise LocalInputError(
                "ENGINE_INPUT_UNSUPPORTED",
                f"{resolved_engine} 不支持本机文件字节输入；请换用支持 URL 的 search_image。",
            )
        return await asyncio.wait_for(
            _execute_search(
                resolved_engine, None, extra_params_json, limit, search_args=search_args,
            ),
            SEARCH_TOTAL_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - 统一交给 _outcome_for_exception 分流
        return _outcome_for_exception(exc, engine)


def _resolve_engine(engine: Optional[str]) -> str:
    if engine is None:
        requested = (os.environ.get(DEFAULT_ENGINE_ENV) or DEFAULT_ENGINE).strip() or DEFAULT_ENGINE
    elif isinstance(engine, str):
        requested = engine.strip()
    else:
        raise SearchInputError("engine 需要字符串或 null")
    if requested.casefold() == ALL_ENGINE.casefold():
        return ALL_ENGINE
    for name in ENGINES:
        if name.casefold() == requested.casefold():
            return name
    raise SearchInputError(f"不支持的引擎 '{requested}'。用 get_engine_info('all') 查看可用列表。")


def _all_extra_params(raw: Any) -> dict[str, dict[str, Any]]:
    """按参数契约分发 All 的参数，先完成整组校验再发出请求。"""
    if not isinstance(raw, dict):
        raise SearchInputError("extra_params_json 顶层必须是对象")
    grouped: dict[str, dict[str, Any]] = {name: {} for name in ENGINES}
    for key, value in raw.items():
        if key in params.RESERVED_KEYS:
            raise SearchInputError(f"'{key}' 是保留参数，不接受调用方传入")
        recipients = [name for name in ENGINES
                      if key in params.CONTRACTS[name].init or key in params.CONTRACTS[name].search]
        if not recipients:
            raise SearchInputError(f"All 不认识参数 '{key}'")
        for name in recipients:
            params.validate_extra_params(name, {key: value})
            grouped[name][key] = value
    return grouped


async def _run_all_search(
    source: Optional[str],
    raw_extra: Optional[str],
    limit: Any,
    *,
    prepared_input: dict[str, Any] | None = None,
) -> SearchOutcome:
    resolved_limit = _resolve_limit(limit)
    grouped = _all_extra_params(_load_extra_params(raw_extra))
    if prepared_input is None:
        if not isinstance(source, str):
            raise SearchInputError(
                "[INPUT_URL_REQUIRED] search_image accepts absolute HTTP(S) image URLs only. "
                "Local paths, file URIs, data URIs, and inline Base64 are unsupported. "
                "This server provides no upload or hosting endpoint."
            )
        trimmed: Optional[str] = _validate_image_url(source)
        engines = dict(ENGINES)
    else:
        trimmed = None
        # 本机模式下只查询公告过支持 file 字节的引擎。
        engines = {name: cls for name, cls in ENGINES.items() if name in LOCAL_FILE_ENGINES}
    semaphore = asyncio.Semaphore(ALL_CONCURRENCY)

    async def run_one(name: str) -> SearchOutcome:
        async with semaphore:
            try:
                if prepared_input is None:
                    assert trimmed is not None
                    call = _run_search(
                        trimmed, name, json.dumps(grouped[name]), resolved_limit,
                        prepared_input=None,
                    )
                else:
                    call = _execute_search(
                        name, None, json.dumps(grouped[name]), resolved_limit,
                        search_args=prepared_input,
                    )
                return await asyncio.wait_for(call, ALL_ENGINE_TIMEOUT)
            except asyncio.TimeoutError:
                return SearchOutcome(text=f"Error: {name} 搜索超时", is_error=True)
            except Exception as exc:  # noqa: BLE001 - 单引擎失败不拖垮整次查询
                return _outcome_for_exception(exc, name)

    tasks = {name: asyncio.create_task(run_one(name)) for name in engines}
    try:
        _, pending = await asyncio.wait(tasks.values(), timeout=SEARCH_TOTAL_TIMEOUT)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        outcomes = [
            (name, SearchOutcome(text=f"Error: {name} 搜索总期限已到", is_error=True)
             if task.cancelled() else task.result())
            for name, task in tasks.items()
        ]
    finally:
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)

    records = []
    results = []
    for name, outcome in outcomes:
        data = outcome.structured or _structured_results(name, [], resolved_limit)
        status = "error" if outcome.is_error else ("success" if data["result_count"] else "no_results")
        records.append({**data, "status": status, "error": outcome.text if outcome.is_error else None})
        results.extend({**item, "engine": name} for item in data["results"])
    all_failed = all(outcome.is_error for _, outcome in outcomes)
    structured = {
        "engine": ALL_ENGINE,
        "result_count": sum(record["result_count"] for record in records),
        "returned": len(results),
        "truncated": any(record["truncated"] for record in records),
        "results": results,
        "engine_outcomes": records,
        "all_failed": all_failed,
    }
    sections = [outcome.text if outcome.text.startswith("Search Engine: ")
                else f"Search Engine: {name}\n{outcome.text}" for name, outcome in outcomes]
    if prepared_input is not None:
        mode = f"{len(outcomes)} local-file engines"
        skipped = [name for name in ENGINES if name not in engines]
        if skipped:
            sections.insert(0, "Skipped engines (no local file support): " + ", ".join(skipped))
    else:
        mode = f"{len(outcomes)} engines"
    return SearchOutcome(
        text=f"Search Engine: All ({mode}, limit {resolved_limit} per engine)\n\n"
             + "\n\n".join(sections),
        structured=structured,
        is_error=all_failed,
    )


async def _run_search(
    source: str,
    engine: str,
    extra_params_json: Optional[str],
    limit: Any,
    *,
    prepared_input: dict[str, Any] | None = None,
) -> SearchOutcome:
    """URL 输入的薄封装；保留这个函数名给调用方与测试做替换点。"""
    return await _execute_search(
        engine, source, extra_params_json, limit, search_args=prepared_input,
    )


async def _execute_search(
    engine: str,
    source: Optional[str],
    extra_params_json: Optional[str],
    limit: Any,
    *,
    search_args: dict[str, Any] | None = None,
) -> SearchOutcome:
    """执行一次引擎查询；``search_args`` 已定好输入时不再解析 URL。"""
    # 先校验输入，再创建网络请求。
    if engine not in ENGINES:
        raise SearchInputError(
            f"不支持的引擎 '{engine}'。用 get_engine_info('all') 查看可用列表。"
        )

    resolved_limit = _resolve_limit(limit)
    raw_extra = _load_extra_params(extra_params_json)
    init_kwargs, search_kwargs = params.validate_extra_params(engine, raw_extra)

    # 纵深防御：即使白名单被绕过/替换，保留键也不能活着走到输入装配。
    for reserved in params.RESERVED_KEYS:
        search_kwargs.pop(reserved, None)

    if search_args is None:
        if not isinstance(source, str):
            raise SearchInputError(f"source 需要字符串，收到 {type(source).__name__}")
        # 2. 准备搜索输入（URL 可能由我们自己抓，见 _prepare_search_input）
        search_args = await _prepare_search_input(engine, source)

    # 3. 凭据与出站配置
    credentials = load_engine_credentials(engine)
    network_kwargs, explicit_proxy = _build_network_kwargs()

    # SauceNAO 的 api_key 只从环境取，不接受调用方传入
    if engine == "SauceNAO":
        api_key = os.environ.get("IMAGE_SEARCH_API_KEY")
        if api_key:
            init_kwargs["api_key"] = api_key

    search_kwargs.update(search_args)

    engine_cls = ENGINES[engine]

    async with Network(**network_kwargs) as net:
        # 移除 Network client 在构造时继承的环境代理，保留显式代理。
        dropped = strip_inherited_proxies(net, explicit_proxy=explicit_proxy is not None)
        if dropped:
            logger.warning(
                "已拆掉从环境继承的代理挂载 %s：本服务只认显式的 %s",
                ", ".join(dropped),
                PROXY_VAR,
            )

        if credentials is not None:
            # 装域限定的 jar：只发给该引擎真正访问的域名。
            # Network 接不了 Cookies 对象，所以装到它交出的真实 client 上。
            install_cookie_jar(net, credentials.jar())
            logger.debug("已为 %s 装载限域 cookies（来源 %s）", engine, credentials.source)

        client = engine_cls(client=net, **init_kwargs)
        response = await client.search(**search_kwargs)

    return _format_response(engine, response, resolved_limit)


_UPSTREAM_ERROR_ATTRS: dict[str, tuple[str, ...]] = {
    # 上游自己报错时把错误存下来的字段（读上游源码确认过的）：
    # TraceMoeResponse.error；SauceNAOResponse.header.status（0 = 正常）。
    "TraceMoe": ("error",),
    "SauceNAO": ("status",),
}


def _redact_upstream(message: str, limit: int = 200) -> str:
    """隐藏上游错误文本中的 Base64 data URI，并限制文本长度。"""
    text = re.sub(
        r"data:[\w.+-]+/[\w.+-]+;base64,[A-Za-z0-9+/=]+", "data:<redacted>", message
    )
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _upstream_error(engine: str, response: Any) -> Optional[str]:
    """读取引擎明确提供的错误字段；没有错误信号时返回 None。"""
    if response is None:
        return None  # 交由调用方按"响应契约错误"处理

    code = getattr(response, "status_code", None)
    if isinstance(code, int) and not isinstance(code, bool) and code >= 400:
        return f"上游 HTTP {code}"

    for attr in _UPSTREAM_ERROR_ATTRS.get(engine, ()):
        value = getattr(response, attr, None)
        if value in (None, "", 0, "0"):
            continue
        return f"上游报错：{_redact_upstream(str(value))}"

    return None


def _format_response(engine: str, response: Any, limit: int) -> SearchOutcome:
    """同时生成文本和结构化搜索结果。

    文本保留引擎专属字段，结构化结果只包含通用字段；不另附 JSON 文本镜像。"""
    lines = [f"Search Engine: {engine}"]

    if response is None or not hasattr(response, "raw"):
        return SearchOutcome(
            text=(
                "Error: 上游响应缺少 raw 字段"
                f"（{type(response).__name__}）—— 这是响应契约错误，"
                "不能当成'没有结果'。"
            ),
            is_error=True,
        )

    upstream_error = _upstream_error(engine, response)
    if upstream_error is not None:
        return SearchOutcome(
            text=f"Error: {engine} 明确报错 —— {upstream_error}",
            is_error=True,
        )

    raw = response.raw
    if raw is None:
        return SearchOutcome(
            text="Error: 上游响应的 raw 是 None —— 这是响应契约错误，不能当成'没有结果'。",
            is_error=True,
        )

    if not raw:
        lines.append("No results found.")
        if engine in ("Yandex", "Google", "Bing", "GoogleLens", "Tineye"):
            # 空结果不提供足够证据判断具体失败原因。
            lines.append(
                "Hint: 可检查图片质量与索引覆盖，或尝试其他引擎。"
                "若上游需要凭据，请配置该引擎的 Cookies（见 README）。"
            )
        # 没结果不是错误：这是一次成功、且上游没有明确报错的搜索。
        return SearchOutcome(
            text="\n".join(lines),
            structured=_structured_results(engine, [], limit),
        )

    shown = min(len(raw), limit)
    truncated = len(raw) > shown
    lines.append(
        f"Found {len(raw)} results (showing top {shown}"
        f"{', truncated' if truncated else ''}):"
    )
    for index, item in enumerate(raw[:shown], start=1):
        lines.append("")
        lines.append(f"--- Result {index} ---")
        lines.append(_format_result_item(item, engine))
    return SearchOutcome(
        text="\n".join(lines),
        structured=_structured_results(engine, raw, limit),
    )


@mcp.tool(
    title="以图搜图",
    annotations=SEARCH_ANNOTATIONS,
    output_schema=SEARCH_OUTPUT_SCHEMA,
)
async def search_image(
    source: Annotated[str, Field(
        description=(
            "Absolute HTTP(S) image URL reachable by the selected fetcher without "
            "additional request credentials, at most 4096 characters. Local paths, "
            "file URIs, data URIs, and inline Base64 are unsupported by this tool."
        ),
    )],
    engine: Optional[str] = None,
    extra_params_json: Optional[str] = None,
    limit: StrictInt = DEFAULT_LIMIT,
) -> ToolResult:
    """
    Reverse image search using an absolute HTTP(S) image URL.

    This tool accepts URLs only. Local paths, file URIs, data URIs, and
    inline Base64 are unsupported. This server provides no upload or hosting endpoint.

    A local image requires an existing, authorized file-to-HTTP workflow that
    produces an image URL reachable by the selected fetcher without additional
    request credentials. Without such a workflow or an existing suitable URL,
    this tool cannot search a local image. A URL reachable only on the agent's
    machine is insufficient.

    BaiDu and EHentai images are fetched by this MCP server under its download
    policy. Other engines receive the URL for upstream retrieval. Image URLs
    or bytes are disclosed to the selected search providers.

    Args:
        source: Absolute HTTP(S) image URL, at most 4096 characters.
        engine: The search engine to use. If omitted or null, use IMAGE_SEARCH_DEFAULT_ENGINE,
                falling back to Yandex. Names are case-insensitive. All queries every engine.
                Other supported engines: SauceNAO, Google, TraceMoe, Ascii2D, EHentai, etc.
                Use the `get_engine_info` tool to see the full list and capabilities.

        extra_params_json: (Optional) JSON string for advanced engine parameters.
                Each engine declares its own parameter set; unknown or reserved
                keys are rejected (use `get_engine_info` to list them).

        limit: Max number of results to return (1-50, default: 5).
    """
    outcome = await _search_image_logic(source, engine, extra_params_json, limit)
    if outcome.structured is not None and "engine_outcomes" in outcome.structured:
        return ToolResult(content=outcome.text, structured_content=outcome.structured,
                          is_error=outcome.is_error)
    if outcome.is_error:
        # 使用 ToolError 返回工具执行错误。
        raise ToolError(outcome.text)
    return ToolResult(content=outcome.text, structured_content=outcome.structured)


# --------------------------------------------------------------------------
# 本机 stdio 模式的额外工具（默认不注册）
# --------------------------------------------------------------------------

#: 已经注册过本机工具的实例 id，避免重复注册。
_LOCAL_REGISTERED_TARGET_IDS: set[int] = set()


async def search_local_image(
    path: Annotated[str, Field(
        description=(
            "Absolute local image path on this machine, at most 4096 characters. "
            "Only available in local stdio mode and only under the server-configured "
            "allowlist directories; symlinks are rejected."
        ),
    )],
    engine: Optional[str] = None,
    extra_params_json: Optional[str] = None,
    limit: StrictInt = DEFAULT_LIMIT,
) -> ToolResult:
    """
    Reverse image search using a local image file (local stdio mode only).

    This tool exists only when the server runs as a local stdio process with
    IMAGE_SEARCH_LOCAL_INPUT=1 and IMAGE_SEARCH_LOCAL_ROOTS pointing at one or
    more allowlist directories. The path must be an absolute regular file
    inside those directories, must not cross a symlink, and must be a JPEG,
    PNG, or static WebP of at most 8 MiB. The file bytes are read once and
    disclosed to the selected search providers; the path itself is not sent.

    HTTP/SSE mode never registers this tool: remote callers must use
    `search_image` with an absolute HTTP(S) URL.

    Args:
        path: Absolute local image path under the server's allowlist roots.
        engine: Same engine names as `search_image`. All queries every engine
                that accepts local file bytes.
        extra_params_json: Same engine parameter contract as `search_image`.
        limit: Max number of results to return (1-50, default: 5).
    """
    outcome = await _search_local_image_logic(path, engine, extra_params_json, limit)
    if outcome.structured is not None and "engine_outcomes" in outcome.structured:
        return ToolResult(content=outcome.text, structured_content=outcome.structured,
                          is_error=outcome.is_error)
    if outcome.is_error:
        raise ToolError(outcome.text)
    return ToolResult(content=outcome.text, structured_content=outcome.structured)


def register_local_tool(target: FastMCP | None = None) -> bool:
    """把 ``search_local_image`` 注册到目标实例。

    未启用本机输入（开关或 allowlist 不成立）时什么都不做并返回 False。
    调用方负责传输模式：``main()`` 只在 stdio 分支调用它，并在 HTTP 模式下
    直接拒绝这种配置。
    """
    if not local_input.enabled():
        return False
    destination = target if target is not None else mcp
    if id(destination) in _LOCAL_REGISTERED_TARGET_IDS:
        return True
    destination.tool(
        title="以图搜图（本机图片）",
        annotations=SEARCH_ANNOTATIONS,
        output_schema=SEARCH_OUTPUT_SCHEMA,
    )(search_local_image)
    _LOCAL_REGISTERED_TARGET_IDS.add(id(destination))
    return True
