"""凭据的归属与出站范围。

== 为什么需要这个模块 ==

早期版本只有一个全局 ``IMAGE_SEARCH_COOKIES``，并且把它原样塞给 ``httpx``：

    AsyncClient(cookies={"sid": "..."})     # 无 domain 的 dict 形式

实测结果是**这个 cookie 会被发给任意域名**，包括与引擎完全无关的站点::

    https://yandex.com/a         Cookie=sid=ENGINE_COOKIE
    https://untrusted.invalid/b  Cookie=sid=ENGINE_COOKIE   ← 也发了
    https://api.trace.moe/c      Cookie=sid=ENGINE_COOKIE   ← 也发了

换 ``httpx.Cookies()`` 显式 ``domain="yandex.com"`` 之后，无关域名拿到的是 ``None``。
所以"限域"是有效的 —— 而"每次新建一个 client"**并不提供隔离**：cookie 本身没有域，
换个 client 它照样跟着走。

== 三条规矩 ==

1. **凭据必须有明确归属。** 全局配置没有归属信息，无法判断它该发给谁 ——
   与其猜，不如拒绝，并告诉调用方怎么改。归属不明就是拒绝（不是"禁用一下继续跑"）。
2. **限域到该引擎真正访问的域名**，由下面的 ``COOKIE_DOMAINS`` 表决定；
   表里的每个域名都由 ``tests/test_credentials.py`` 对着上游源码核验。
3. **代理不接受隐式来源。** 早期版本会退回读 ``HTTP_PROXY``/``HTTPS_PROXY`` ——
   用户没配过它也会命中，而带凭据的请求悄悄走一个不知从哪来的代理是不可接受的行为。
   现在只认显式的 ``IMAGE_SEARCH_PROXY``；检测到通用代理变量时会明确提示被忽略。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from http.cookiejar import Cookie
from typing import Mapping
from urllib.parse import urlparse

import httpx

# --------------------------------------------------------------------------
# 引擎 → 凭据应发往的域名
#
# 这张表来自对上游各引擎源码里出现的 HTTP 主机名的实测（见测试里的核验）。
# 取"父域"而不是精确主机：httpx 的 domain= 会连子域一起匹配
# （domain="trace.moe" 覆盖 api.trace.moe，domain="iqdb.org" 覆盖 3d.iqdb.org）。
# --------------------------------------------------------------------------

COOKIE_DOMAINS: dict[str, tuple[str, ...]] = {
    "Yandex": ("yandex.com",),
    "SauceNAO": ("saucenao.com",),
    "Ascii2D": ("ascii2d.net",),
    "TraceMoe": ("trace.moe",),          # 子域 api.trace.moe 由父域覆盖
    "EHentai": ("e-hentai.org", "exhentai.org"),   # 两个站点是独立域，须各列
    "Google": ("google.com", "google.co.jp"),
    "GoogleLens": ("google.com",),       # lens.google.com / www.google.com 同父域
    "BaiDu": ("baidu.com",),             # graph.baidu.com 同父域
    "Bing": ("bing.com",),
    "Iqdb": ("iqdb.org",),               # 3d.iqdb.org 同父域
    "Tineye": ("tineye.com",),
}

#: 引擎专属凭据环境变量的前缀与后缀
COOKIE_VAR_PREFIX = "IMAGE_SEARCH_COOKIES"
#: 全局凭据变量（无归属）—— 使用时必须同时给出归属
LEGACY_COOKIE_VAR = "IMAGE_SEARCH_COOKIES"
#: 声明全局凭据归属的变量
LEGACY_OWNER_VAR = "IMAGE_SEARCH_COOKIES_ENGINE"
#: 显式代理（唯一被接受的代理来源）
PROXY_VAR = "IMAGE_SEARCH_PROXY"
#: 这些变量**不再被使用**，出现时给出提示
IGNORED_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")

#: cookie 名称必须是 token 字符。
#: 旧写法 ``^[^=;]+=[^=;]*$`` 一处错两个方向：名称那半允许空格与控制字符
#: （``bad name=x`` 会被接受），值那半又禁掉 ``=``，而 base64/URL 编码的值里
#: ``=`` 很常见（``sid=YWJjZA==`` 被误拒）。
#: 上游按**第一个** ``=`` 拆（``PicImageSearch/network.py:51-54``），这里跟它对齐。
_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
#: 值里禁止控制字符与片段分隔符 ``;``；``=`` 允许保留。
_COOKIE_VALUE_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f;]")


class CredentialError(ValueError):
    """凭据配置有歧义或不合法。"""

    def __init__(self, message: str, *, hint: str = ""):
        self.hint = hint
        super().__init__(message)


# --------------------------------------------------------------------------
# cookie 解析（按库的契约返回字符串，不返回 dict）
# --------------------------------------------------------------------------

def normalize_cookie_string(raw: str, *, engine: str) -> str:
    """校验并规范化 cookie 串，返回**字符串**。

    上游 ``Network.__init__`` 对 cookies 参数调用 ``.split(";")`` —— 它要的是字符串。
    早期版本这里返回 ``dict``，于是任何**非空**配置都必然抛
    ``AttributeError: 'dict' object has no attribute 'split'``：
    照 README 配置的人 100% 失败。

    这里同时做格式校验，因为上游自己的解析是
    ``line.strip().split("=", 1)`` —— 遇到不带 ``=`` 的片段会直接抛 ValueError，
    报错信息里看不出是配置问题。提前校验能给出可操作的提示。
    """
    if not isinstance(raw, str):
        raise CredentialError(
            f"{engine} 的 cookies 必须是字符串，收到 {type(raw).__name__}",
            hint="格式为 'name=value; name2=value2'",
        )

    # 换行可以让值突破单行 —— 不要把控制字符放进请求头
    if any(ch in raw for ch in "\r\n"):
        raise CredentialError(
            f"{engine} 的 cookies 含换行符，拒绝使用",
            hint="每个 cookie 之间用 '; ' 分隔，不要换行",
        )

    segments = [s.strip() for s in raw.split(";")]
    segments = [s for s in segments if s]
    if not segments:
        raise CredentialError(f"{engine} 的 cookies 是空串", hint="要么留空，要么写 'name=value'")

    for index, segment in enumerate(segments, start=1):
        name, sep, value = segment.partition("=")
        # 错误信息只给**片段序号**，不回显片段内容：旧写法把整个 ``segment!r``
        # 拼进消息，而它会经 server.py 的错误出口返回给调用方，
        # 等于把凭据值写进对方日志（A 报告 A4）。
        if not sep:
            raise CredentialError(
                f"{engine} 的 cookies 第 {index} 个片段不带 '='，缺少值",
                hint="格式为 'name=value; name2=value2'",
            )
        if not _COOKIE_NAME.match(name):
            raise CredentialError(
                f"{engine} 的 cookies 第 {index} 个片段的名称不合法",
                hint="名称只能是 token 字符（字母、数字与 !#$%&'*+-.^_`|~）",
            )
        if not value.isascii():
            raise CredentialError(
                f"{engine} 的 cookies 第 {index} 个片段的值含非 ASCII 字符",
                hint="值必须能原样放进请求头；需要就先用 base64 或 URL 编码",
            )
        if _COOKIE_VALUE_FORBIDDEN.search(value):
            raise CredentialError(
                f"{engine} 的 cookies 第 {index} 个片段的值含控制字符或 ';'",
                hint="值里不要放控制字符或分号",
            )

    return "; ".join(segments)


def build_cookie_jar(cookie_string: str, *, domains: tuple[str, ...]) -> httpx.Cookies:
    """构造**限域且仅 HTTPS** 的 cookie jar。

    这两个都不是默认行为，必须显式做：

    1. **限域**：用 ``httpx.Cookies().set(..., domain=...)`` 而不是 ``cookies={...}`` ——
       后者产生的 cookie 没有域，会跟着请求发往任何域名。
    2. **仅 HTTPS**：``Cookies.set()`` 内部构造的 ``Cookie`` 是 ``secure=False``，
       也就是 ``http://yandex.com/`` 这个明文地址也会带上凭据（复核报告 B4，已实测）。
       ``Cookies.set()`` **没有** secure 参数，所以这里直接建 ``http.cookiejar.Cookie``
       把 ``secure`` 打开。代价是引擎若走 http:// 就拿不到 cookie —— 这正是想要的结果。
    """
    jar = httpx.Cookies()
    for segment in cookie_string.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        name, _, value = segment.partition("=")
        for domain in domains:
            jar.jar.set_cookie(
                Cookie(
                    version=0,
                    name=name.strip(),
                    value=value.strip(),
                    port=None,
                    port_specified=False,
                    domain=domain,
                    domain_specified=True,
                    domain_initial_dot=False,
                    path="/",
                    path_specified=True,
                    secure=True,
                    expires=None,
                    discard=True,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False,
                )
            )
    return jar


def strip_inherited_proxies(
    client: httpx.AsyncClient,
    *,
    explicit_proxy: bool = False,
) -> tuple[str, ...]:
    """拆掉 httpx **构造时从环境变量继承**来的代理挂载，返回被拆掉的 pattern。

    == 为什么不能靠 trust_env ==

    ``trust_env`` 是**构造参数**。httpx 在 ``AsyncClient.__init__`` 里就把
    ``get_environment_proxies()`` 编译成 URLPattern 挂进 ``_mounts``
    （``httpx/_client.py:242-249``），而 ``_transport_for_url`` 之后**只看** ``_mounts``
    （同文件 760-769）。所以在别人构造好的 client 上再改 ``_trust_env`` 是无效的 ——
    这样一来"环境代理不生效"这个承诺在端到端上原本是空的：带凭据的引擎请求会
    悄悄走一个不知从哪来的代理（复核报告 B5）。

    == 为什么判定依据是"我们自己配没配代理" ==

    不能靠"看到 mount 就拆"：显式 ``proxy=`` 也会挂出形状完全一样的 ``all://`` mount，
    两者在对象上看不出区别。唯一可靠的依据是我们自己的配置：
    只有**没有**配 ``IMAGE_SEARCH_PROXY`` 时，client 上的代理才只可能来自环境。

    ``value 为 None`` 的 mount 不是代理，它表示"用默认 transport"，不动它。
    """
    if explicit_proxy:
        return ()

    mounts = getattr(client, "_mounts", None)
    if not mounts:
        return ()

    removed: list[str] = []
    for key in list(mounts):
        if mounts[key] is None:
            continue
        del mounts[key]
        removed.append(getattr(key, "pattern", str(key)))
    return tuple(removed)


def cookie_domains_for(engine: str) -> tuple[str, ...]:
    if engine not in COOKIE_DOMAINS:
        raise CredentialError(f"引擎 {engine!r} 没有凭据域名定义", hint="补进 COOKIE_DOMAINS")
    return COOKIE_DOMAINS[engine]


# --------------------------------------------------------------------------
# 配置读取
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class EngineCredentials:
    """某个引擎的凭据，已限定归属。"""

    engine: str
    cookie_string: str
    #: 这段 cookie 是谁配的（用于错误信息与审计）
    source: str

    def jar(self) -> httpx.Cookies:
        return build_cookie_jar(self.cookie_string, domains=cookie_domains_for(self.engine))


def load_engine_credentials(
    engine: str,
    env: Mapping[str, str] | None = None,
) -> EngineCredentials | None:
    """读出该引擎的凭据，或返回 None（表示没配）。

    **归属不明的全局配置会被拒绝**，不是静默忽略也不猜测。原因很直接：
    没有归属就不知道该发给谁，发错了就是凭据泄露。
    """
    env = os.environ if env is None else env

    scoped_var = f"{COOKIE_VAR_PREFIX}_{engine.upper()}"
    scoped = env.get(scoped_var)
    if scoped:
        return EngineCredentials(
            engine=engine,
            cookie_string=normalize_cookie_string(scoped, engine=engine),
            source=scoped_var,
        )

    legacy = env.get(LEGACY_COOKIE_VAR)
    if not legacy:
        return None

    owner = (env.get(LEGACY_OWNER_VAR) or "").strip()
    if not owner:
        raise CredentialError(
            f"检测到 {LEGACY_COOKIE_VAR}，但它没有归属 —— 无法判断这些 cookie 该发给谁。",
            hint=(f"改名为 {scoped_var}（该引擎专属），"
                  f"或补上 {LEGACY_OWNER_VAR}={engine} 指明归属。"),
        )

    if owner.lower() != engine.lower():
        # 有归属，但不属于本次调用的引擎 —— 只是不适用，不是错误
        return None

    return EngineCredentials(
        engine=engine,
        cookie_string=normalize_cookie_string(legacy, engine=engine),
        source=f"{LEGACY_COOKIE_VAR}（归属 {owner}）",
    )


# --------------------------------------------------------------------------
# 代理
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ProxyConfig:
    url: str
    source: str


def _described_proxy(raw: str) -> str:
    """给错误信息用的代理描述：**只留结构与是否带凭据，绝不含 userinfo**。

    ``http://user:pass@host`` 的原文里就写着密码，而 CredentialError 会被
    server.py 回给工具调用方。cookie 那条同类路径当时已经脱敏，proxy 这条漏了
    （复核报告：错误代理 URL 回显 secret）。连 URL 都解析不出来的输入，
    更没有理由把原文倒回去。
    """
    try:
        parsed = urlparse(raw)
    except ValueError:
        return "<无法解析，原文已隐去>"
    where = parsed.hostname or "<无主机名>"
    try:
        if parsed.port:
            where = f"{where}:{parsed.port}"
    except ValueError:
        where = f"{where}:<端口不合法>"
    creds = "含凭据（已隐去）" if (parsed.username or parsed.password) else "无凭据"
    return f"{parsed.scheme or '?'}://{where}（{creds}）"


def load_proxy(env: Mapping[str, str] | None = None) -> ProxyConfig | None:
    """读出显式配置的代理。

    **不读** ``HTTP_PROXY`` / ``HTTPS_PROXY``。早期版本会退回读它们，结果是：
    用户从没配过代理，却因为环境里有这个变量而让带凭据的请求走了某个代理；
    而且那条路径本身是坏的（传 dict 给只吃字符串的参数，直接抛
    ``AttributeError: 'dict' object has no attribute 'url'``）。现在只认显式配置。

    所有报错都只回 ``_described_proxy`` 的结构化描述；原文（可能含密码）不出现
    在任何异常消息里。
    """
    env = os.environ if env is None else env

    raw = (env.get(PROXY_VAR) or "").strip()
    if not raw:
        return None

    try:
        parsed = urlparse(raw)
    except ValueError:
        raise CredentialError(
            f"{PROXY_VAR} 不是合法的 URL：{_described_proxy(raw)}",
            hint="例如 http://127.0.0.1:7890；IPv6 主机要写成 [::1] 这种带方括号的形式",
        ) from None

    if parsed.scheme not in ("http", "https", "socks5", "socks5h"):
        raise CredentialError(
            f"{PROXY_VAR} 的 scheme {parsed.scheme!r} 不受支持",
            hint="允许 http / https / socks5 / socks5h，例如 http://127.0.0.1:7890",
        )
    if not parsed.hostname:
        raise CredentialError(
            f"{PROXY_VAR} 缺少主机名：{_described_proxy(raw)}",
            hint="例如 http://127.0.0.1:7890",
        )

    # 端口非法（非数字、0、超范围）以前会一路漏到 httpx 才炸，被兜底成"内部错误"——
    # 那等于把一个纯配置问题说成我们的 bug。这里当场归成配置错。
    try:
        port = parsed.port
    except ValueError:
        raise CredentialError(
            f"{PROXY_VAR} 的端口不合法：{_described_proxy(raw)}",
            hint="端口要在 1-65535 之间，例如 http://127.0.0.1:7890",
        ) from None
    if port is not None and not (1 <= port <= 65535):
        raise CredentialError(
            f"{PROXY_VAR} 的端口超出范围：{_described_proxy(raw)}",
            hint="端口要在 1-65535 之间，例如 http://127.0.0.1:7890",
        )

    return ProxyConfig(url=raw, source=PROXY_VAR)


def ignored_proxy_vars(env: Mapping[str, str] | None = None) -> list[str]:
    """返回存在但被忽略的通用代理变量名（用于提示，不用于取配置）。"""
    env = os.environ if env is None else env
    return [name for name in IGNORED_PROXY_VARS if (env.get(name) or "").strip()]


# --------------------------------------------------------------------------
# 装到 client 上
# --------------------------------------------------------------------------

def install_cookie_jar(client: httpx.AsyncClient, jar: httpx.Cookies) -> None:
    """把限域 jar 装到真正的 httpx client 上。

    注意装的位置：``Network`` 本身**接不了** jar —— 它对 cookies 调 ``.split()``::

        Network(cookies=CookieJar) → AttributeError: 'Cookies' object has no attribute 'split'

    可行做法是：``Network`` 不接收 cookie 字符串，等它交出内部的真实 client
    （``async with Network(...) as net`` 的 ``net`` 就是这个 client），再往它上面装域限定的 jar。
    """
    client.cookies.update(jar)
