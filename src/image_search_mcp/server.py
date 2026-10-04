"""MCP 工具实现。

== 与本模块相关的历史缺陷 ==

1. **服务端任意文件读取**（最严重）。旧代码在把 ``file``/``url`` 装好之后执行
   ``search_kwargs.update(extra_params)``，于是调用方能用一个 ``{"file": "/etc/passwd"}``
   覆盖输入；上游 ``utils.read_file()`` 接受路径字符串并直接 ``open()``，
   读出来的内容还会被 multipart 上传到引擎站点（本机与远端各验证过一次）。
   现在 ``extra_params`` 走 :mod:`image_search_mcp.params` 的白名单，保留键一律拒绝。

2. **拿 dict 当字符串传**。旧的 ``_parse_cookies()`` / ``_parse_proxy()`` 返回 dict，
   而 ``Network.__init__`` 对它们调用 ``.split()`` / ``.url`` —— 任何**非空**配置必炸，
   而 README 恰好把这两个变量写成"遇到验证就这样配"。现在按库的契约传字符串，
   且 cookie 限域（见 :mod:`image_search_mcp.credentials`）。

3. **凭据无归属、无域**。旧代码把 cookie 塞给 ``httpx`` 的无域 dict 形式，
   实测会发往任意域名。现在按引擎取凭据、限域到该引擎真正访问的域名。

4. **traceback 回给调用方**。旧代码把完整堆栈（含服务端绝对路径）拼进返回值。
   现在详细堆栈只进日志。

5. **本机替用户抓图无任何校验**（EHentai / BaiDu）。现在改由
   :mod:`image_search_mcp.safe_download` 接管，引擎只拿到字节。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations

# 为什么**不再**兜底导入 ``mcp.server.fastmcp``：mcp 2.x 已经把它改名成
# ``mcp.server.mcpserver.MCPServer``（API 也变了）。那条兜底路径在当前依赖下
# 只会抛 "No module named 'mcp.server.fastmcp'"，把"没装 fastmcp"这个真实原因
# 掩盖成一条看不出所以然的错误。pyproject 里 ``fastmcp>=4.0`` 是硬依赖，
# 所以直接硬导入：缺了就是缺了，报错也要报得能看懂。

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

from . import params
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

#: 输入图片解码后的上限（Base64 长度按 4/3 反推，留一点余量）
MAX_IMAGE_BYTES = 16 * 1024 * 1024

_DATA_URI_PREFIX = "data:"


class SearchInputError(ValueError):
    """调用方输入不合法。与"上游出错"区分开，便于调用方判断该不该重试。"""


@dataclass(frozen=True)
class SearchOutcome:
    """一次搜索的结果，同时带着"给人看的文本"和"给机器看的结构"。

    为什么不让 ``_search_image_logic`` 直接返回字符串：那样工具层就无法区分
    "搜到了但没结果"与"搜索失败"，而 MCP 规范要求把后者用 ``isError: true``
    返回，客户端和模型才能据此自我修正（复核报告 A 的
    "no result 与 search failed 不可区分"）。
    """

    text: str
    structured: dict[str, Any] | None = None
    is_error: bool = False


#: 搜索工具的输出契约。
#: 只声明**各引擎通用**的四个字段 —— 字段少而形状确定，比字段多而形状不定更有用：
#: 输出 schema 一旦虚设，客户端就没法真的依赖它。引擎特有字段（episode、
#: author_url 等）仍然只在文本里。
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

#: 搜索是**只读**且会访问外部服务；不声明这两点，客户端只能一律当危险操作处理。
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
    """相似度原样保留，只挡掉布尔与复杂对象。

    ``isinstance(True, int)`` 为真，所以布尔要先排除 —— 否则 ``True`` 会变成
    ``"True%"`` 那种东西进到结构化输出里。
    """
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
        lines = ["Supported Search Engines:"]
        for name in params.CONTRACTS:
            lines.append(f"- {name}: {params.engine_brief(name)}")
        return "\n".join(lines)

    for name in params.CONTRACTS:
        if name.lower() == engine_name.lower():
            return params.engine_details(name)

    # 按 MCP 规范，"输入不对、模型可以自己改"的失败属于工具执行错误 → isError=true。
    # 旧写法把 "Error: ..." 当**正常结果**返回，客户端无法与真正的结果区分。
    # 用 ToolError 而不是让 ValueError 冒出去：后者的消息会被 fastmcp 加上
    # "Error calling tool 'x': " 前缀（实测），对模型只是噪音。
    raise ToolError(
        f"Engine '{engine_name}' not found. Supported: {', '.join(params.CONTRACTS)}"
    )


# --------------------------------------------------------------------------
# 结果格式化
# --------------------------------------------------------------------------

def _format_result_item(item: Any, engine: str) -> str:
    """把单个结果项格式化成文本。

    注意两个容易写错的地方：

    - **零值要被当成有值**。``episode=0`` 与 ``To=0`` 在旧代码里被真值判断吞掉：
      ``if item.episode`` 对 0 为假，集数直接消失；``end_time if end_time else '?'``
      把 0 显示成问号。判断"有没有值"必须用 ``is not None``。
    - **不要在这里丢字段**。旧代码的 SauceNAO 分支不读 ``similarity`` 与
      ``author_url``，而同一份对象交给 Iqdb 分支时 ``similarity`` 是会输出的 ——
      同一类信息在不同引擎下时有时无。
    """
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

def _decode_image_source(source: str) -> bytes:
    """把 Base64 / data URI 解码成字节，并做长度与合法性校验。"""
    if not isinstance(source, str) or not source.strip():
        raise SearchInputError("source 为空")

    payload = source.strip()
    if payload.lower().startswith(_DATA_URI_PREFIX):
        # 只接受 "data:<mediatype>;base64,<payload>" 这一种形态
        if "," not in payload:
            raise SearchInputError("data URI 缺少 ',' 分隔符")
        header, payload = payload.split(",", 1)
        if ";base64" not in header.lower():
            raise SearchInputError("data URI 必须是 base64 编码（缺少 ';base64'）")

    # 反推上限，避免先把超长输入整个解码进内存
    if len(payload) > (MAX_IMAGE_BYTES // 3 + 1) * 4:
        raise SearchInputError(f"Base64 输入超过上限（解码后上限 {MAX_IMAGE_BYTES} 字节）")

    try:
        decoded = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SearchInputError(
            "source 既不是 http(s) URL，也不是合法 Base64；"
            f"解码失败：{exc}"
        ) from exc

    if not decoded:
        raise SearchInputError("解码后的图片数据为空")

    if len(decoded) > MAX_IMAGE_BYTES:
        raise SearchInputError(f"图片数据超过上限 {MAX_IMAGE_BYTES} 字节")

    return decoded


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
    """解析 ``extra_params_json``。

    三种"没有参数"的写法都接受：字段省掉、空字符串、以及 JSON 字面量 ``null``。
    客户端把可选字段序列化成 ``null`` 是常见做法，为此报错没有意义。
    **但数组和标量不在此列** —— 那些是调用方搞错了形态，必须报错而不是当成空。
    """
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
    """组装 Network 的构造参数。

    ``Network`` 的 ``cookies`` / ``proxies`` 都只吃**字符串**：

    - cookies 会被 ``cookies.split(";")`` 处理
    - proxies 会直接交给 httpx 的 ``proxy=`` 参数

    旧代码两边都传 dict，于是任何非空配置必然抛
    ``AttributeError: 'dict' object has no attribute 'split'`` / ``no attribute 'url'``。

    注意这里**不传 cookies** —— cookie 需要限域，而 ``Network`` 接不了域限定的
    ``Cookies`` 对象（它对入参调 ``.split()``）。做法是等它交出内部真实 client 之后再装。
    """
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
    """构造取图下载器。

    单独抽一个工厂，是为了给测试留一个**清晰的注入点** ——
    测试要注入受控解析器与连接层，才能观察"连接层实际收到的目的地址"
    （只断言 URL 字符串是测不到 DNS 那一步的）。
    """
    return SafeDownloader()


async def _prepare_search_input(
    engine: str,
    source: str,
) -> dict[str, Any]:
    """决定这次搜索给引擎什么输入。

    **本机抓图的引擎（EHentai / BaiDu）不走引擎内部那条无校验的下载路径** ——
    我们自己用 :class:`SafeDownloader` 把它取回来，再把字节交给引擎。
    其余引擎把 URL 直接交给搜索站点去抓，那不是本机的出站面。

    决策标准只认 `ENGINES_THAT_FETCH_LOCALLY`，它由测试对着上游源码核验。
    """
    trimmed = source.strip()

    if not trimmed.lower().startswith(("http://", "https://")):
        return {"file": _decode_image_source(trimmed)}

    if engine in ENGINES_THAT_FETCH_LOCALLY:
        downloader = _make_downloader()
        data = await downloader.fetch(trimmed)
        return {"file": data}

    return {"url": trimmed}


async def _search_image_logic(
    source: str,
    engine: str = "Yandex",
    extra_params_json: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
) -> SearchOutcome:
    """图片搜索的核心逻辑（与 MCP 工具层分开，便于直接测试）。

    **失败不抛，而是带 ``is_error`` 返回**：工具层要把它翻成 MCP 的
    ``isError: true``，而"让异常冒出去"那条路的文本会被 fastmcp 加上
    ``Error calling tool 'x': `` 前缀（实测），对模型只是噪音。

    错误文本保留 ``Error: `` 前缀是**刻意的**：这样只看文本的客户端行为不变，
    协议感知的客户端才拿 ``isError`` 做判断 —— 协议升级应当是加法。
    """
    try:
        return await _run_search(source, engine, extra_params_json, limit)
    except SearchInputError as exc:
        return SearchOutcome(text=f"Error: {exc}", is_error=True)
    except params.ParamError as exc:
        return SearchOutcome(text=f"Error: {exc}", is_error=True)
    except CredentialError as exc:
        message = f"Error: {exc}"
        if exc.hint:
            message += f"\nHint: {exc.hint}"
        return SearchOutcome(text=message, is_error=True)
    except DownloadError as exc:
        return SearchOutcome(
            text=f"Error: 取图失败 [{exc.reason}] {exc}", is_error=True
        )
    except Exception as exc:  # noqa: BLE001 - 兜底：对外只给简短信息，细节进日志
        # 旧代码把 traceback.format_exc() 拼进返回值 —— 那会泄露服务端绝对路径，
        # 而且对调用方没有用处。详细堆栈留在日志里。
        #
        # 这里刻意**不拼接 str(exc)**：异常消息里可能带文件路径、URL、凭据片段。
        # 只留异常类型名，够定位是哪一类问题了。
        logger.exception("图片搜索失败 engine=%s", engine)
        return SearchOutcome(
            text=(
                f"Error: 搜索过程中发生内部错误（{type(exc).__name__}）。"
                "详细信息见服务端日志。"
            ),
            is_error=True,
        )


async def _run_search(
    source: str,
    engine: str,
    extra_params_json: Optional[str],
    limit: Any,
) -> SearchOutcome:
    # 1. 先校验调用方输入 —— 顺序很重要：**任何拒绝都要发生在读文件与建网络之前**
    #
    # 引擎名校验排在最前：它比"参数契约不存在"更能说清问题出在哪，
    # 也不该让一个拼错的引擎名以别的名字报出来。
    if engine not in ENGINES:
        raise SearchInputError(
            f"不支持的引擎 '{engine}'。用 get_engine_info('all') 查看可用列表。"
        )

    resolved_limit = _resolve_limit(limit)
    raw_extra = _load_extra_params(extra_params_json)
    init_kwargs, search_kwargs = params.validate_extra_params(engine, raw_extra)

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
        # httpx 默认 trust_env=True，而且在**构造时**就把环境里的代理挂进了 _mounts，
        # 事后改 trust_env 是无效的（见 credentials.strip_inherited_proxies 的说明）。
        # 不拆掉的话，"环境代理不生效"这个承诺是空的 —— 带凭据的请求会走一个
        # 不知从哪来的代理（复核报告 B5）。
        dropped = strip_inherited_proxies(net, explicit_proxy=explicit_proxy is not None)
        if dropped:
            logger.warning(
                "已拆掉从环境继承的代理挂载 %s：本服务只认显式的 %s",
                ", ".join(dropped),
                PROXY_VAR,
            )

        if credentials is not None:
            # 装**域限定**的 jar：只发给该引擎真正访问的域名。
            # Network 接不了 Cookies 对象，所以装到它交出的真实 client 上。
            install_cookie_jar(net, credentials.jar())
            logger.debug("已为 %s 装载限域 cookies（来源 %s）", engine, credentials.source)

        client = engine_cls(client=net, **init_kwargs)
        response = await client.search(**search_kwargs)

    return _format_response(engine, response, resolved_limit)


def _format_response(engine: str, response: Any, limit: int) -> SearchOutcome:
    """把上游响应同时整理成"人读文本"和"机器可读结构"。

    关于规范里那条"返回结构化内容的工具 SHOULD 同时把序列化 JSON 放进文本块"：
    它是为了纯文本客户端不丢信息。这里的文本本来就是同一份数据的完整渲染
    （每个字段都出现了），再塞一份 JSON 只是把 payload 翻倍。所以不重复塞。
    """
    lines = [f"Search Engine: {engine}"]

    raw = getattr(response, "raw", None)
    if not raw:
        lines.append("No results found.")
        if engine in ("Yandex", "Google", "Bing", "GoogleLens", "Tineye"):
            lines.append(
                f"Hint: '{engine}' 常需要配置该引擎的 cookies 才能绕过机器人验证"
                f"（见 README 的 Cookies 章节）。"
            )
        # **没结果不是错误**：这是一次成功的搜索，只是上游没给结果。
        # 标成 isError 会让模型去"修"一个并不存在的问题。
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
    source: str,
    engine: str = "Yandex",
    extra_params_json: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
) -> ToolResult:
    """
    Perform a reverse image search.

    Args:
        source: The image input.
                1. If you have a public image URL, use the URL.
                2. If you have image data, use a Base64 encoded string
                   (a `data:image/...;base64,...` data URI also works).
                3. Do NOT provide a local file path — the server does not read
                   local files, and such input is rejected.

        engine: The search engine to use (default: "Yandex").
                Other supported engines: SauceNAO, Google, TraceMoe, Ascii2D, EHentai, etc.
                Use the `get_engine_info` tool to see the full list and capabilities.

        extra_params_json: (Optional) JSON string for advanced engine parameters.
                Each engine declares its own parameter set; unknown or reserved
                keys are rejected (use `get_engine_info` to list them).

        limit: Max number of results to return (1-50, default: 5).
    """
    outcome = await _search_image_logic(source, engine, extra_params_json, limit)
    if outcome.is_error:
        # 工具执行错误 → 按 MCP 规范用 isError=true 交回给调用方，让它/模型能自我修正。
        # 用 ToolError 而不是让裸异常冒出去：后者的文本会被加上
        # 「Error calling tool 'search_image': 」前缀（实测），对模型只是噪音。
        raise ToolError(outcome.text)
    return ToolResult(content=outcome.text, structured_content=outcome.structured)
