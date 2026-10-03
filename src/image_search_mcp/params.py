"""参数契约：本服务对调用方承诺的参数范围 —— 单一事实来源。

== 这个模块解决什么 ==

上游每个引擎的 ``search()`` 都带 ``**kwargs``，所以**任何拼错的参数名都会被静默吞掉**：
调用方以为设了 ``cutBorders``，实际发出去的仍是默认值，而且没有任何提示。
更早的版本还把无效参数写进了对外公示（Yandex 的 ``rpt``/``cbir_page``、TraceMoe 的
``cutBorders``、SauceNAO 的 ``output_type``），于是"公示了"被当成"支持了"。

本模块的每一项都来自**对已安装 PicImageSearch 的真实签名检查**，不是从 README 或
旧的 ``ENGINE_INFO`` 手抄。核验方式见 ``tests/test_param_contract.py``：
它把这里的契约与 ``inspect.signature`` 逐项比对，上游改签名时会先响。

== 三条规矩 ==

1. **白名单之外一律拒绝**，不静默吞掉。
2. **保留键永远不接受用户输入**（``url``/``file``/``client``/``api_key``/``request_kwargs``）。
   早期版本在装好 ``file``/``url`` 之后执行 ``search_kwargs.update(extra_params)``，
   使得 ``file`` 能被覆盖成任意本地路径 —— 那是服务端任意文件读取。
3. **对外公示由本模块生成**，不做手写副本，避免文档与行为各自漂移。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


class ParamError(ValueError):
    """调用方参数不合契约。

    带上足够定位的信息：哪个引擎、哪个键、为什么拒、允许什么。
    """

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
    """一个参数的类型与取值约束。

    ``type`` 只取 ``bool`` / ``int`` / ``str``。注意 **bool 必须先于 int 判断** ——
    Python 里 ``isinstance(True, int)`` 为真，反过来会把布尔值放进整数参数。
    """

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
    """一个引擎对外的参数面。

    ``init`` 是构造参数（构造 ``Engine(client=..., **init)`` 时传入）；
    ``search`` 是搜索参数（``Engine.search(**search)`` 时传入）。
    """

    init: Mapping[str, Spec] = field(default_factory=dict)
    search: Mapping[str, Spec] = field(default_factory=dict)
    #: supported / experimental / deprecated —— 用于对外如实公示支持状态
    status: str = "supported"
    note: str = ""


# --------------------------------------------------------------------------
# 契约本体
#
# 每个条目的 inclusion/exclusion 理由都写在旁边。**排除项也是契约的一部分** ——
# 否则下一个人会以为漏了。
# --------------------------------------------------------------------------

CONTRACTS: dict[str, EngineContract] = {
    # Yandex.__init__(base_url, request_kwargs)
    # Yandex.search(url, file, **kwargs)
    # 排除 rpt / cbir_page：签名里没有它们；上游在 engines/yandex.py 内写死
    #   {"rpt": "imageview", "cbir_page": "sites"}，用户传什么都会被覆盖 ——
    #   旧版把它们公示成"高级参数"，调用方设了不生效。
    "Yandex": EngineContract(),

    # SauceNAO.__init__(base_url, api_key, numres, hide, minsim, output_type,
    #                   testmode, dbmask, dbmaski, db, dbs, request_kwargs)
    # 排除 output_type：engines/saucenao.py 把 output_type 放进请求参数，但解析
    #   永远走 json_loads(resp.text)；设成非 2 会让 SauceNAO 返回 HTML 而解析失败。
    #   即"这个参数存在，但这个库用不了它"。
    # 排除 dbs / dbmask / dbmaski：与 db 重叠，面窄，本批不开。
    # 排除 api_key：凭据走环境变量，不接受调用方传入。
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

    # TraceMoe.__init__(base_url, base_url_api, mute, size, request_kwargs)
    # TraceMoe.search(url, file, key, anilist_id, chinese_title, cut_borders, **kwargs)
    #
    # 命名坑：对外 Python 参数名是 **cut_borders**（下划线）。旧版公示的是
    #   ``cutBorders``，那是 HTTP 查询串里的名字；传进来会被 **kwargs 吞掉，
    #   出站请求仍是默认的 cutBorders=true。
    #
    # 上游缺陷（已实测，不在本服务修复范围）：``chinese_title`` 参数存在且默认为 True，
    #   engines/tracemoe.py 里会执行 item.title_chinese = anime_info["title"].get("chinese","")，
    #   但模块级 ANIME_INFO_QUERY 的 GraphQL 只请求 native/romaji/english —
    #   接口不会返回 chinese，所以该字段恒为空串。**该参数目前是空操作。**
    # 排除 key：那是 trace.moe 的 API Key，属凭据，走环境变量的路线由使用方决定。
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

    # GoogleLens.__init__(base_url, search_url, search_type, q, hl, country, request_kwargs)
    # GoogleLens.search(url, file, q, **kwargs)
    # 排除 search_type / hl / country：取值集合未逐个核验，不放进白名单 ——
    #   "没核验过的取值"不该对外承诺。
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
    # Iqdb.search(url, file, force_gray, **kwargs)
    "Iqdb": EngineContract(
        init={
            "is_3d": Spec(bool, False, "搜索 3D 站点"),
        },
        search={
            "force_gray": Spec(bool, False, "转灰度后搜索"),
        },
    ),

    # Tineye.__init__(base_url, request_kwargs)
    # Tineye.search(url, file, show_unavailable_domains, domain, sort, order, tags, **kwargs)
    # 只开 show_unavailable_domains：sort/order/tags 的取值集合未核验。
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
    """把调用方给的 extra params 按契约拆成 (init_kwargs, search_kwargs)。

    任何不合契约的输入都抛 :class:`ParamError`，**绝不静默忽略**。这一点是刻意的：
    上游每个 ``search()`` 都有 ``**kwargs``，静默吞掉是它的默认行为，靠它就等于没有契约。

    顶层必须是对象（JSON object）。数组、null、标量都拒绝 —— 早期版本直接
    ``search_kwargs.update(extra_params)``，顶层类型不对时会以各种方式穿透到下游。
    """
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
    "Yandex": "综合能力最强的通用搜图引擎，对裁剪、翻转和修改过的图片识别率极高。",
    "SauceNAO": "专注二次元插画、漫画和动漫截图搜索。Pixiv 图片识别率极高。需要 API Key。",
    "Ascii2D": "专注二次元插画，特别适合查找 Twitter 和 Pixiv 上的原始画师。支持颜色搜索和特征搜索。",
    "TraceMoe": "动漫截图专用搜索引擎，可识别具体番剧名称、集数和时间点。",
    "EHentai": "专门搜索 E-Hentai 和 ExHentai 的图库。搜索 ExHentai 需要在环境中配置该引擎的 cookies。",
    "Google": "谷歌通用搜图。适合寻找类似图片或图片来源。",
    "GoogleLens": "谷歌智慧镜头。擅长识别物体、文字和商品。",
    "BaiDu": "百度识图，国内资源识别较好。",
    "Bing": "必应视觉搜索。",
    "Iqdb": "多站聚合搜索 (Danbooru, Konachan, etc.)，适合二次元图片。",
    "Tineye": "老牌反向搜图引擎，擅长寻找精确匹配的图片来源。",
}

_STATUS_LABEL = {
    "supported": "",
    "experimental": "[实验性] ",
    "deprecated": "[已弃用] ",
}


def engine_brief(engine: str) -> str:
    contract = CONTRACTS[engine]
    label = _STATUS_LABEL.get(contract.status, "")
    text = f"{label}{_BRIEFS.get(engine, engine)}支持情况：[URL: 是, 文件: 是]。"
    if contract.note:
        text += contract.note
    return text


def engine_details(engine: str) -> str:
    """单个引擎的详情文本。参数清单由契约生成，手写副本必然漂移。"""
    contract = CONTRACTS[engine]
    lines = [f"=== {engine} ===", f"Brief: {engine_brief(engine)}"]

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
