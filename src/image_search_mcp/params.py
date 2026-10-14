"""各引擎支持的参数、类型和取值范围，以及对应的校验与说明生成函数。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


class ParamError(ValueError):
    """参数校验错误，包含引擎名和参数名。"""

    def __init__(self, message: str, *, engine: str = "", key: str = ""):
        self.engine = engine
        self.key = key
        self.reason = message
        super().__init__(message)


# --------------------------------------------------------------------------
# 保留键：任何情况下都不接受来自调用方的值
# --------------------------------------------------------------------------

#: 这些键由服务端自己决定。``url``/``file`` 是搜索输入本体；``client`` 是 Network 实例；
#: ``api_key``/``cookies``/``proxies`` 属于凭据与出站策略；``base_url`` 会改变请求目标。
RESERVED_KEYS: frozenset[str] = frozenset({
    "url",
    "file",
    "client",
    "api_key",
    "cookies",
    "proxies",
    "proxy",
    "request_kwargs",
    "base_url",
    "base_url_api",
    "kwargs",
})


# --------------------------------------------------------------------------
# 契约描述
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Spec:
    """单个参数的类型、默认值和取值限制。"""

    type: type
    default: Any
    desc: str
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] | None = None

    def check(self, value: Any, *, engine: str, key: str) -> Any:
        if self.type is bool:
            if not isinstance(value, bool):
                raise ParamError(
                    f"'{key}' 需要布尔值（true/false），收到 {type(value).__name__}",
                    engine=engine, key=key,
                )
            return value

        if self.type is int:
            # bool 是 int 的子类，这里必须显式排除
            if isinstance(value, bool) or not isinstance(value, int):
                raise ParamError(
                    f"'{key}' 需要整数，收到 {type(value).__name__}",
                    engine=engine, key=key,
                )
            if self.minimum is not None and value < self.minimum:
                raise ParamError(
                    f"'{key}' 不能小于 {self.minimum}（收到 {value}）",
                    engine=engine, key=key,
                )
            if self.maximum is not None and value > self.maximum:
                raise ParamError(
                    f"'{key}' 不能大于 {self.maximum}（收到 {value}）",
                    engine=engine, key=key,
                )
            return value

        if self.type is str:
            if not isinstance(value, str):
                raise ParamError(
                    f"'{key}' 需要字符串，收到 {type(value).__name__}",
                    engine=engine, key=key,
                )
            if self.choices is not None and value not in self.choices:
                raise ParamError(
                    f"'{key}' 只接受 {list(self.choices)}，收到 {value!r}",
                    engine=engine, key=key,
                )
            return value

        raise ParamError(f"契约内部错误：{key} 的类型未支持", engine=engine, key=key)


@dataclass(frozen=True)
class EngineContract:
    """引擎的构造参数、搜索参数与支持状态。"""

    init: Mapping[str, Spec] = field(default_factory=dict)
    search: Mapping[str, Spec] = field(default_factory=dict)
    #: supported / experimental / deprecated —— 用于对外如实公示支持状态
    status: str = "supported"
    note: str = ""


# --------------------------------------------------------------------------
# 保留不开放参数的原因，便于后续调整支持范围。
# --------------------------------------------------------------------------

CONTRACTS: dict[str, EngineContract] = {
    # Yandex 的 rpt / cbir_page 由上游固定，不作为可配置参数。
    "Yandex": EngineContract(),

    # output_type 固定为 JSON；数据库位掩码暂未开放；api_key 从环境读取。
    "SauceNAO": EngineContract(
        init={
            "numres": Spec(int, 5, "返回条数上限", minimum=1, maximum=40),
            "hide": Spec(int, 0, "过滤等级 (0=不过滤 1=explicit 2=questionable 3=safe)",
                         minimum=0, maximum=3),
            "minsim": Spec(int, 30, "最低相似度百分比", minimum=0, maximum=100),
            "db": Spec(int, 999, "指定数据库 ID (999 = 全部)", minimum=1),
            "testmode": Spec(int, 0, "测试模式", minimum=0, maximum=1),
        },
    ),

    # Ascii2D.__init__(base_url, bovw, request_kwargs)
    "Ascii2D": EngineContract(
        init={
            "bovw": Spec(bool, False,
                         "true = 特征搜索（对裁剪/改图更有效）；false = 颜色搜索"),
        },
    ),

    # 参数名使用 cut_borders。chinese_title 在当前上游查询中不产生中文标题。
    "TraceMoe": EngineContract(
        search={
            "cut_borders": Spec(bool, True, "裁掉黑边"),
            "chinese_title": Spec(bool, True,
                                  "请求中文标题（上游缺陷：GraphQL query 未请求该字段，当前为空操作）"),
            "anilist_id": Spec(int, 0, "限定 AniList ID (0 = 不限)", minimum=0),
        },
    ),

    # EHentai.__init__(is_ex, covers, similar, exp, request_kwargs)
    "EHentai": EngineContract(
        init={
            "is_ex": Spec(bool, False, "搜索 ExHentai（需要 cookies）"),
            "covers": Spec(bool, False, "只搜封面"),
            "similar": Spec(bool, True, "启用相似度扫描"),
            "exp": Spec(bool, False, "包含已删除的画廊"),
        },
    ),

    # Google.__init__(base_url, request_kwargs)
    # 上游 main 已把该模块标记为 DEPRECATED（PyPI 3.12.11 仍可用）。
    "Google": EngineContract(
        status="deprecated",
        note="上游已将 Google 引擎标记为弃用，后续版本可能移除；建议改用 GoogleLens。",
    ),

    # 只开放查询词 q；其他参数的取值范围尚未验证。
    "GoogleLens": EngineContract(
        init={
            "q": Spec(str, "", "附加查询词"),
        },
        search={
            "q": Spec(str, "", "附加查询词（覆盖 init 里的）"),
        },
    ),

    # BaiDu.__init__(request_kwargs)
    "BaiDu": EngineContract(),

    # Bing.__init__(request_kwargs)
    # 上游 main 补了签名解密（+73 行），但 PyPI 3.12.11 没有；相关 issue 长期未闭。
    "Bing": EngineContract(
        status="experimental",
        note="上游 3.12.11 缺签名解密修复（该修复尚未发版），可能返回空结果。",
    ),

    # Iqdb.__init__(is_3d, request_kwargs)
    # Iqdb.search(url, file, force_gray, kwargs)
    "Iqdb": EngineContract(
        init={
            "is_3d": Spec(bool, False, "搜索 3D 站点"),
        },
        search={
            "force_gray": Spec(bool, False, "转灰度后搜索"),
        },
    ),

    # 只开放 show_unavailable_domains；排序与标签参数暂未验证。
    "Tineye": EngineContract(
        search={
            "show_unavailable_domains": Spec(bool, False, "包含当前不可用的站点"),
        },
    ),
}

ENGINE_NAMES: tuple[str, ...] = tuple(CONTRACTS)


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------

def _describe_allowed(contract: EngineContract) -> str:
    parts = []
    for name, spec in contract.init.items():
        parts.append(f"{name}(init)")
    for name, spec in contract.search.items():
        parts.append(f"{name}(search)")
    return ", ".join(parts) if parts else "（无）"


def validate_extra_params(
    engine: str,
    raw: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """校验附加参数，并拆分为 (init_kwargs, search_kwargs)。

    None 表示未提供参数；其余输入必须是对象，未知键和保留键均被拒绝。"""
    if raw is None:
        return {}, {}

    if not isinstance(raw, dict):
        raise ParamError(
            f"extra params 顶层必须是对象（key-value），收到 {type(raw).__name__}",
            engine=engine,
        )

    contract = CONTRACTS.get(engine)
    if contract is None:
        raise ParamError(f"未知引擎 {engine!r}，无参数契约可用", engine=engine)

    init_kwargs: dict[str, Any] = {}
    search_kwargs: dict[str, Any] = {}

    for key, value in raw.items():
        if not isinstance(key, str):
            raise ParamError(f"参数名必须是字符串，收到 {type(key).__name__}", engine=engine)

        if key in RESERVED_KEYS:
            # 这一条是安全边界，不是风格问题：早期版本允许它，于是 file 能被
            # 覆盖成任意本地路径并被上传到引擎站点。
            raise ParamError(
                f"'{key}' 是保留参数，不接受调用方传入（它由服务端决定）",
                engine=engine, key=key,
            )

        if key in contract.init:
            init_kwargs[key] = contract.init[key].check(value, engine=engine, key=key)
            continue

        if key in contract.search:
            search_kwargs[key] = contract.search[key].check(value, engine=engine, key=key)
            continue

        raise ParamError(
            f"'{engine}' 不认识参数 '{key}'。允许：{_describe_allowed(contract)}",
            engine=engine, key=key,
        )

    return init_kwargs, search_kwargs


# --------------------------------------------------------------------------
# 对外公示（生成，不手写）
# --------------------------------------------------------------------------

_BRIEFS: dict[str, str] = {
    "Yandex": "通用图片来源与相似图片搜索。",
    "SauceNAO": "插画、漫画与动漫图片来源查询，可配置 SauceNAO API Key。",
    "Ascii2D": "插画来源查询，支持颜色搜索和特征搜索。",
    "TraceMoe": "动漫截图专用搜索引擎，可识别具体番剧名称、集数和时间点。",
    "EHentai": "专门搜索 E-Hentai 和 ExHentai 的图库。搜索 ExHentai 需要在环境中配置该引擎的 cookies。",
    "Google": "谷歌通用搜图。适合寻找类似图片或图片来源。",
    "GoogleLens": "Google Lens 图片搜索。",
    "BaiDu": "百度识图。",
    "Bing": "必应视觉搜索。",
    "Iqdb": "多站聚合搜索 (Danbooru, Konachan, etc.)，适合二次元图片。",
    "Tineye": "图片来源与重复图片搜索。",
}

_STATUS_LABEL = {
    "supported": "",
    "experimental": "[实验性] ",
    "deprecated": "[已弃用] ",
}


def engine_brief(engine: str, *, local_enabled: bool = False) -> str:
    contract = CONTRACTS[engine]
    label = _STATUS_LABEL.get(contract.status, "")
    if local_enabled:
        inputs = (
            "输入：绝对 HTTP(S) 图片 URL；本机 stdio 模式下还可用 search_local_image 读 allowlist 内的本地路径。"
        )
    else:
        inputs = "输入：仅绝对 HTTP(S) 图片 URL。"
    text = f"{label}{_BRIEFS.get(engine, engine)}{inputs}"
    if contract.note:
        text += contract.note
    return text


def engine_details(engine: str, *, local_enabled: bool = False) -> str:
    """根据参数定义生成单个引擎的说明。"""
    contract = CONTRACTS[engine]
    lines = [f"=== {engine} ===", f"Brief: {engine_brief(engine, local_enabled=local_enabled)}"]

    if contract.init or contract.search:
        lines.append("")
        lines.append("Advanced Parameters (pass via 'extra_params_json'):")
        for name, spec in contract.init.items():
            lines.append(f"- {name} (init, {_type_name(spec)}): {spec.desc}"
                         f" [默认 {spec.default!r}{_range_text(spec)}]")
        for name, spec in contract.search.items():
            lines.append(f"- {name} (search, {_type_name(spec)}): {spec.desc}"
                         f" [默认 {spec.default!r}{_range_text(spec)}]")
    else:
        lines.append("")
        lines.append("No specific advanced parameters.")

    if contract.status != "supported":
        lines.append("")
        lines.append(f"Status: {contract.status}")
    return "\n".join(lines)


def _type_name(spec: Spec) -> str:
    return {bool: "bool", int: "int", str: "str"}.get(spec.type, spec.type.__name__)


def _range_text(spec: Spec) -> str:
    if spec.choices:
        return f", 取值 {list(spec.choices)}"
    if spec.minimum is not None and spec.maximum is not None:
        return f", 范围 {spec.minimum}-{spec.maximum}"
    if spec.minimum is not None:
        return f", 最小 {spec.minimum}"
    if spec.maximum is not None:
        return f", 最大 {spec.maximum}"
    return ""
