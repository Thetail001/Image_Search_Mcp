"""凭据归属与出站范围。

核心断言只有一句：**引擎的 cookie 只能发给该引擎真正访问的域名。**

围绕它有三组容易漏掉的反向检查：

- 用**真实的 ``Network``** 而不是 Mock 来验（把构造换成宽松 Mock，
  就永远发现不了"应用层把入参类型重塑了"这件事 —— 那正是旧代码的 bug）
- 不仅验"传字符串"，还验"字符串 + 无域 cookie"这条更隐蔽的形态
  （只还原 dict 实现的话，类型测试会红，但泄露不会）
- 正向也要有：正确引擎必须真的收到凭据 —— 否则"把 cookie 功能整个删掉"也能通过
"""

from __future__ import annotations

import inspect

import httpx
import pytest
from PicImageSearch import engines as upstream_engines
from PicImageSearch import Network

from image_search_mcp import credentials
from image_search_mcp.credentials import (
    COOKIE_DOMAINS,
    CredentialError,
    build_cookie_jar,
    cookie_domains_for,
    install_cookie_jar,
    load_engine_credentials,
    load_proxy,
    normalize_cookie_string,
)

ENGINE_MODULES = {
    "SauceNAO": "saucenao", "Google": "google", "TraceMoe": "tracemoe",
    "Ascii2D": "ascii2d", "BaiDu": "baidu", "Bing": "bing", "EHentai": "ehentai",
    "GoogleLens": "google_lens", "Iqdb": "iqdb", "Tineye": "tineye", "Yandex": "yandex",
}


# ==========================================================================
# 域名表 vs 上游源码
# ==========================================================================

@pytest.mark.parametrize("engine", sorted(COOKIE_DOMAINS))
def test_declared_domains_appear_in_upstream_source(engine: str):
    """凭据域名表必须来自上游真实访问的主机 —— 逐条对着源码核验。

    表里少一个域，那个站点的凭据就发不出去（功能坏）；
    多一个域，凭据就发给了不该发的站点（安全问题）。两种都不能靠记忆维护。
    """
    source = inspect.getsource(
        __import__(f"PicImageSearch.engines.{ENGINE_MODULES[engine]}", fromlist=["_"])
    )
    for domain in COOKIE_DOMAINS[engine]:
        assert domain in source, (
            f"{engine} 的凭据域名表写了 {domain!r}，但上游源码里根本没有这个主机名。"
            f"要么表写错了，要么引擎换了站点。"
        )


def test_domain_table_covers_every_engine():
    from image_search_mcp.server import ENGINES
    assert set(COOKIE_DOMAINS) == set(ENGINES)


def test_ehentai_lists_both_sites():
    """E-Hentai 与 ExHentai 是两个独立域，必须各列 —— 少一个就有一个站点用不上凭据。"""
    assert set(cookie_domains_for("EHentai")) == {"e-hentai.org", "exhentai.org"}


# ==========================================================================
# cookie 串规范化
# ==========================================================================

def test_cookie_string_is_normalized_to_a_string():
    """上游 Network 对 cookies 调 .split(";") —— 必须给字符串。

    旧代码返回 dict，于是任何**非空**配置都必然抛
    ``AttributeError: 'dict' object has no attribute 'split'``。
    """
    result = normalize_cookie_string("sid=abc;  other=1 ", engine="Yandex")
    assert isinstance(result, str)
    assert result == "sid=abc; other=1"


@pytest.mark.parametrize("bad,keyword", [
    ("novalue", "不是 'name=value'"),
    ("a=1; badsegment", "不是 'name=value'"),
    ("", "空串"),
    ("   ", "空串"),
    ("a=1\nb=2", "换行"),
])
def test_bad_cookie_strings_are_rejected_with_actionable_message(bad: str, keyword: str):
    with pytest.raises(CredentialError) as exc:
        normalize_cookie_string(bad, engine="Yandex")
    assert keyword in str(exc.value)


def test_dict_input_is_rejected_explicitly():
    with pytest.raises(CredentialError, match="必须是字符串"):
        normalize_cookie_string({"sid": "x"}, engine="Yandex")  # type: ignore[arg-type]


# ==========================================================================
# 归属
# ==========================================================================

def test_engine_scoped_variable_is_used():
    creds = load_engine_credentials("Yandex", {"IMAGE_SEARCH_COOKIES_YANDEX": "sid=Y"})
    assert creds is not None
    assert creds.engine == "Yandex"
    assert creds.cookie_string == "sid=Y"


def test_global_cookie_without_owner_is_refused():
    """归属不明就拒绝 —— 不是"忽略它继续跑"。

    没有归属信息时，任何"猜一个引擎"的做法都可能把凭据发错地方。
    """
    with pytest.raises(CredentialError) as exc:
        load_engine_credentials("Yandex", {"IMAGE_SEARCH_COOKIES": "sid=X"})
    assert "没有归属" in str(exc.value)
    assert "IMAGE_SEARCH_COOKIES_YANDEX" in exc.value.hint


def test_global_cookie_with_matching_owner_is_accepted():
    creds = load_engine_credentials(
        "Yandex",
        {"IMAGE_SEARCH_COOKIES": "sid=X", "IMAGE_SEARCH_COOKIES_ENGINE": "Yandex"},
    )
    assert creds is not None and creds.cookie_string == "sid=X"


def test_global_cookie_belonging_to_another_engine_is_not_applied():
    """有归属但不属于本次调用的引擎 → 不使用（不是错误，而是不适用）。"""
    creds = load_engine_credentials(
        "Yandex",
        {"IMAGE_SEARCH_COOKIES": "sid=X", "IMAGE_SEARCH_COOKIES_ENGINE": "EHentai"},
    )
    assert creds is None


def test_no_cookies_configured_returns_none():
    assert load_engine_credentials("Yandex", {}) is None


def test_scoped_variable_wins_over_global():
    creds = load_engine_credentials("Yandex", {
        "IMAGE_SEARCH_COOKIES": "sid=GLOBAL",
        "IMAGE_SEARCH_COOKIES_ENGINE": "Yandex",
        "IMAGE_SEARCH_COOKIES_YANDEX": "sid=SCOPED",
    })
    assert creds is not None and creds.cookie_string == "sid=SCOPED"


# ==========================================================================
# 代理
# ==========================================================================

def test_explicit_proxy_is_accepted():
    proxy = load_proxy({"IMAGE_SEARCH_PROXY": "http://127.0.0.1:7890"})
    assert proxy is not None and proxy.url == "http://127.0.0.1:7890"


@pytest.mark.parametrize("bad", ["ftp://x:1", "127.0.0.1:7890", "http://"])
def test_bad_proxy_is_rejected(bad: str):
    with pytest.raises(CredentialError):
        load_proxy({"IMAGE_SEARCH_PROXY": bad})


def test_generic_proxy_env_vars_are_not_used():
    """HTTP_PROXY / HTTPS_PROXY 不再被隐式采用。

    旧代码会退回读它们：用户没配过代理，带凭据的请求却悄悄走了环境里的代理；
    而且那条路径本身是坏的（给只吃字符串的参数传 dict，必然抛异常）。
    """
    assert load_proxy({"HTTP_PROXY": "http://192.0.2.1:3128"}) is None
    assert load_proxy({"HTTPS_PROXY": "http://192.0.2.1:3128"}) is None
    assert credentials.ignored_proxy_vars(
        {"HTTP_PROXY": "http://192.0.2.1:3128"}
    ) == ["HTTP_PROXY"]


def test_empty_proxy_is_none():
    assert load_proxy({"IMAGE_SEARCH_PROXY": "   "}) is None


# ==========================================================================
# 域限定的 jar —— 本文件的核心
# ==========================================================================

@pytest.fixture
def recorder(mock_http):
    """记录出站请求的 cookie，走真实的 httpx client。"""
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("cookie")))
        return httpx.Response(200, text="{}")

    mock_http(handler)
    return seen


async def test_scoped_jar_only_sends_to_the_engine_domain(recorder):
    """用**真实的 Network** 验：域限定的 jar 只发给该引擎的域。

    刻意不 Mock Network —— 旧 bug 正是"应用层把入参类型重塑了"，
    用宽松 Mock 会把它遮住。
    """
    jar = build_cookie_jar("sid=ENGINE_SECRET", domains=cookie_domains_for("Yandex"))

    async with Network() as net:          # 真实 Network，非 Mock
        install_cookie_jar(net, jar)
        await net.get("https://yandex.com/a")
        await net.get("https://untrusted.invalid/b")
        await net.get("https://api.trace.moe/c")

    sent = dict(recorder)
    assert sent["https://yandex.com/a"] == "sid=ENGINE_SECRET", "本域必须收到凭据"
    assert sent["https://untrusted.invalid/b"] is None, "无关域名不能收到凭据"
    assert sent["https://api.trace.moe/c"] is None, "别的引擎域也不能收到"


async def test_domain_scoping_also_covers_subdomains(recorder):
    """domain= 会连子域一起匹配 —— 这是想要的行为（api.trace.moe 要用 trace.moe 的凭据）。"""
    jar = build_cookie_jar("sid=T", domains=cookie_domains_for("TraceMoe"))
    async with Network() as net:
        install_cookie_jar(net, jar)
        await net.get("https://api.trace.moe/search")
    assert dict(recorder)["https://api.trace.moe/search"] == "sid=T"


async def test_unscoped_cookie_string_leaks_to_every_domain(recorder):
    """反向证据：**无域** cookie 会发给任何域名。

    这条是"为什么必须用 Cookies().set(domain=...)"的现场证据 ——
    同时也说明"每次新建一个 client"并不提供隔离（cookie 本身没域，换 client 照样跟着走）。
    """
    async with Network(cookies="sid=LEAK") as net:
        await net.get("https://yandex.com/a")
        await net.get("https://untrusted.invalid/b")

    for url, cookie in recorder:
        assert cookie == "sid=LEAK", f"无域 cookie 发到了 {url}"


async def test_install_cookie_jar_actually_lands_on_the_client(recorder):
    """``Network`` 接不了 Cookies 对象 —— 所以要装到它交出的真实 client 上。"""
    jar = build_cookie_jar("sid=Z", domains=("yandex.com",))
    async with Network() as net:
        install_cookie_jar(net, jar)
        assert any(c.name == "sid" for c in net.cookies.jar)
        await net.get("https://yandex.com/x")
    assert dict(recorder)["https://yandex.com/x"] == "sid=Z"


def test_network_rejects_a_cookies_object():
    """把这条上游限制记成测试：它决定了我们只能"装到 client 上"。

    上游 ``Network.__init__`` 对 cookies 调 ``.split()``，接不了 ``Cookies``。
    等哪天它改了这个行为，这条测试会红，那时才可以简化安装方式。
    """
    with pytest.raises(AttributeError, match="split"):
        Network(cookies=build_cookie_jar("sid=1", domains=("yandex.com",)))  # type: ignore[arg-type]
