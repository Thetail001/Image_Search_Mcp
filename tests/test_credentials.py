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
import re

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
    strip_inherited_proxies,
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
    ("novalue", "缺少值"),
    ("a=1; badsegment", "缺少值"),
    ("", "空串"),
    ("   ", "空串"),
    ("a=1\nb=2", "换行"),
])
def test_bad_cookie_strings_are_rejected_with_actionable_message(bad: str, keyword: str):
    with pytest.raises(CredentialError) as exc:
        normalize_cookie_string(bad, engine="Yandex")
    assert keyword in str(exc.value)


@pytest.mark.parametrize("good", [
    "sid=YWJjZA==",  # base64 值里的 '=' 合法：上游按**第一个** '=' 拆
    "sid=a=b",
    "sid=plain",
    "sid=",
    "a=1; b=2",
])
def test_cookie_values_may_contain_equals(good: str):
    """含 ``=`` 的值必须接受，且**原样保留**（不只是"没抛异常"）。

    旧校验是 ``^[^=;]+=[^=;]*$``，把值里的 ``=`` 一并禁掉 ——
    base64 编码的凭据（``sid=YWJjZA==``）配上就被误拒，而 base64 是最常见的形态。
    """
    result = normalize_cookie_string(good, engine="Yandex")
    for segment in (s.strip() for s in good.split(";")):
        if segment:
            assert segment in result


def test_cookie_error_does_not_echo_credential_value():
    """错误信息不得回显凭据值 —— 它会经 server 的错误出口返回给调用方。

    旧实现把整段 ``segment!r`` 拼进消息，等于把 cookie 值写进对方的日志。
    """
    secret = "SUPER-SECRET-TOKEN-VALUE"
    with pytest.raises(CredentialError) as exc:
        normalize_cookie_string(f"bad name={secret}", engine="Yandex")
    message = str(exc.value)
    assert secret not in message
    assert "第 1 个片段" in message  # 至少要能定位是哪个片段


@pytest.mark.parametrize("bad", [
    "bad name=x",      # 名称含空格（旧校验放行）
    "sid=has\x00nul",  # 值含 NUL（旧校验放行）
    "sid\x01=x",       # 名称含控制字符
    "sid=café",        # 非 ASCII：到构造请求头时才炸，必须提前拒
])
def test_malformed_cookie_segments_are_rejected(bad: str):
    with pytest.raises(CredentialError):
        normalize_cookie_string(bad, engine="Yandex")


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


def test_proxy_error_does_not_echo_the_secret():
    """错误信息不得回显代理解析原文 —— 它会经 server 的错误出口给调用方。

    ``http://user:pass@`` 的原文里就写着密码。cookie 那条同类路径当时已经脱敏，
    代理这条漏了（复核报告：错误代理 URL 回显 secret）。这里连用户名也不回显：
    保留"哪一类问题"的信息就够定位，具体值没有理由外传。
    """
    secret = "SYNTHETIC_PROXY_SECRET"
    with pytest.raises(CredentialError) as exc:
        load_proxy({"IMAGE_SEARCH_PROXY": f"http://user:{secret}@"})

    message = str(exc.value)
    assert secret not in message
    assert "user" not in message
    assert "无主机名" in message      # 仍要能定位问题类别
    assert "已隐去" in message        # 并说明原文为何不出现


@pytest.mark.parametrize(
    "bad",
    [
        "http://[::1",       # IPv6 方括号不配对：urlparse 自己就抛 ValueError
        "http://h:abc",      # 端口非数字
        "http://h:0",        # 端口 0
        "http://h:70000",    # 端口超出 1-65535
    ],
)
def test_proxy_shape_errors_are_config_errors_not_internal_errors(bad: str):
    """坏形状要当场归成配置错。

    旧实现不查端口，非法端口一路漏到 httpx 才炸，被兜底成"内部错误（...）"——
    那等于把纯配置问题说成我们的 bug，调用方拿到的是无从下手的类型名。
    """
    with pytest.raises(CredentialError) as exc:
        load_proxy({"IMAGE_SEARCH_PROXY": bad})
    assert exc.value.hint, "配置错要给可操作的提示"


def test_valid_proxy_with_credentials_still_works():
    """正例：脱敏只作用于错误信息，不能顺手把能用的代理弄坏。

    ``load_proxy`` 必须原样交出 URL —— 带凭据的代理本来就是支持的用法，
    下游要用它建连接。
    """
    raw = "http://user:pw@127.0.0.1:7890"
    proxy = load_proxy({"IMAGE_SEARCH_PROXY": raw})
    assert proxy is not None
    assert proxy.url == raw
    assert proxy.source == "IMAGE_SEARCH_PROXY"


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


# ==========================================================================
# 域表的**反向**核验：少一个域也要红
# ==========================================================================

_HOST_IN_SOURCE = re.compile(r"https?://([A-Za-z0-9._-]+)")

#: 上游会访问、但**明确不该收到引擎凭据**的主机。
#: 每加一项就少一道"凭据发错地方"的护栏，所以必须写清理由。
HOSTS_THAT_NEED_NO_CREDENTIALS = {
    # GoogleLens 的第三方搜索 API：它有自己的 API key，与 Google 账号 cookie 无关。
    # 把 Google 的 cookie 发给它属于凭据外发。
    "www.searchapi.io",
}


@pytest.mark.parametrize("engine", sorted(COOKIE_DOMAINS))
def test_every_upstream_host_is_declared_or_explicitly_excused(engine: str) -> None:
    """源码里出现的每个主机都必须有归属。

    只验"表里的域在源码里存在"是**单向**的：那样从表里**删掉**一个域
    （比如 Google 的 google.co.jp）测试照样通过，而那批凭据就再也发不出去了
    —— 功能坏了却没人知道（复核报告 B6）。这里把方向反过来。
    """
    source = inspect.getsource(
        __import__(f"PicImageSearch.engines.{ENGINE_MODULES[engine]}", fromlist=["_"])
    )
    hosts = {match.group(1) for match in _HOST_IN_SOURCE.finditer(source)}
    declared = COOKIE_DOMAINS[engine]

    undeclared = sorted(
        host
        for host in hosts
        if host not in HOSTS_THAT_NEED_NO_CREDENTIALS
        and not any(host == domain or host.endswith("." + domain) for domain in declared)
    )

    assert not undeclared, (
        f"{engine} 的上游源码访问了 {undeclared}，但凭据域名表里既没有它们，"
        f"也没在 HOSTS_THAT_NEED_NO_CREDENTIALS 里给出理由。"
        f"少一个域等于那个站点的凭据发不出去。"
    )


# ==========================================================================
# B4：凭据只在 HTTPS 上发送
# ==========================================================================

def test_cookie_jar_only_sends_over_https() -> None:
    """凭据不能跟着明文 ``http://`` 走。

    ``Cookies.set()`` 内部造出的 ``Cookie`` 是 ``secure=False``，于是
    ``http://yandex.com/`` 这个明文地址也会带上 cookie（复核报告 B4，已实测）。
    """
    jar = build_cookie_jar("sid=SECRET", domains=("yandex.com",))

    over_http = httpx.Request("GET", "http://yandex.com/a", cookies=jar)
    over_https = httpx.Request("GET", "https://yandex.com/a", cookies=jar)

    assert over_http.headers.get("cookie") is None, "明文请求不能带凭据"
    assert over_https.headers.get("cookie") == "sid=SECRET", "HTTPS 必须带上"


def test_cookie_jar_entries_are_marked_secure() -> None:
    jar = build_cookie_jar("sid=SECRET", domains=("yandex.com",))

    cookies = list(jar.jar)
    assert cookies, "jar 不该是空的"
    assert all(cookie.secure for cookie in cookies)


def test_default_cookie_object_is_not_secure_which_is_why_we_build_our_own() -> None:
    """反向证据：``Cookies.set()`` 的默认行为就是会跟着 ``http://`` 走。

    这条是"为什么不用 ``Cookies.set()``"的现场证据 —— 它也说明
    "把地址校验做成 https-only"这件事光靠调用方自觉是不够的。
    """
    default = httpx.Cookies()
    default.set("sid", "X", domain="yandex.com")

    over_http = httpx.Request("GET", "http://yandex.com/a", cookies=default)

    assert over_http.headers.get("cookie") == "sid=X"


# ==========================================================================
# B5：环境代理必须**真的**不生效
# ==========================================================================

def _proxy_mounts(client: httpx.AsyncClient) -> set[str]:
    mounts = getattr(client, "_mounts", {})
    return {getattr(key, "pattern", str(key)) for key, value in mounts.items() if value is not None}


async def test_inherited_env_proxy_mounts_are_removed(monkeypatch, real_client_init) -> None:
    """必须用**真实 client** 来验。

    默认夹具会给 client 注入 transport，而 httpx 一旦拿到 transport 就**不读**环境代理
    （``allow_env_proxies = trust_env and transport is None``），所以那种写法
    把实现改回 ``trust_env=True`` 也照样通过 —— 就是复核报告 B6 说的假测试。
    这里用 ``real_client_init`` 拿到真实构造路径。
    """
    monkeypatch.setenv("HTTPS_PROXY", "http://192.0.2.1:3128")

    async with httpx.AsyncClient() as client:
        assert _proxy_mounts(client), "前提：httpx 确实先把环境代理挂上了"

        dropped = strip_inherited_proxies(client, explicit_proxy=False)

        assert dropped, "应当报出被拆掉的 pattern"
        assert not _proxy_mounts(client), "拆完之后不能再有代理挂载"


async def test_explicit_proxy_mounts_are_left_alone(real_client_init) -> None:
    """显式配的代理不能被拆。

    两种 mount 在对象上长得**完全一样**（都是 ``all://`` 指向一个代理 transport），
    所以判定依据只能是我们自己的配置，不能靠"看到 mount 就拆"。
    """
    async with httpx.AsyncClient(proxy="http://127.0.0.1:7890") as client:
        before = _proxy_mounts(client)
        assert before, "前提：显式代理确实挂上了"

        dropped = strip_inherited_proxies(client, explicit_proxy=True)

        assert dropped == ()
        assert _proxy_mounts(client) == before


async def test_no_proxy_anywhere_is_a_no_op(monkeypatch, real_client_init) -> None:
    """正对照：没有代理挂载时什么都不动（顺便证明我们不是"一律清空"）。"""
    for var in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)

    async with httpx.AsyncClient() as client:
        assert not _proxy_mounts(client), "前提：干净环境里本来就没有代理挂载"
        assert strip_inherited_proxies(client, explicit_proxy=False) == ()
