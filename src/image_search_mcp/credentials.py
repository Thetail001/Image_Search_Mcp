"""按引擎读取凭据，构造域名受限的 HTTPS Cookie jar，并校验显式代理配置。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from http.cookiejar import Cookie
from typing import Mapping
from urllib.parse import urlparse

import httpx

# --------------------------------------------------------------------------
# 域名表按上游请求目标维护；父域同时匹配子域。
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
#: 这些变量不再被使用，出现时给出提示
IGNORED_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")

#: Cookie 名称只接受 token 字符，值允许包含等号。
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
    """校验 Cookie 名称和值，返回规范化的字符串。错误信息不包含凭据值。"""
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
        # 错误信息仅给出片段序号，避免回显凭据。
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
    """构造仅匹配指定域名、仅通过 HTTPS 发送的 Cookie jar。"""
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
    """移除 httpx 在构造时继承的环境代理，保留显式配置的代理。

    httpx 已将代理写入 _mounts，事后修改 trust_env 不会改变路由。
    此处依赖 httpx 的内部属性，升级时需验证代理集成测试。"""
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
    """优先读取引擎专属 Cookies，再读取归属明确的旧全局配置。未配置时返回 None。"""
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
    """生成不含用户名和密码的代理描述，用于配置错误提示。"""
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
    """读取并校验 IMAGE_SEARCH_PROXY，不使用通用代理变量。"""
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
    """将 Cookie jar 装到 Network 上下文返回的 httpx client。

    Network 的 cookies 参数只接受字符串，无法直接接收域名受限的 jar。"""
    client.cookies.update(jar)
