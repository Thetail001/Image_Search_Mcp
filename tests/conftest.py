"""测试夹具。

== 两条默认姿态 ==

1. **清空环境变量**：本服务的配置全从 ``IMAGE_SEARCH_*`` 读。开发机或 CI 上残留一个
   变量就会让测试行为漂移（实测过：非空 cookie 会把整条搜索路径带到另一条分支上）。
   每个测试开始前把这些变量清掉，需要时由测试自己设。

2. **默认禁网**：没有显式注册 handler 的测试**不允许发出真实 HTTP 请求**。
   这里给 ``httpx`` 注入一个"拒绝一切"的 transport；测试要模拟网络就通过
   ``mock_http`` 注册 handler。这样"忘记 mock"会立刻报错，而不是去访问真实站点 ——
   后者会让测试慢、不稳定，还可能触发对端风控。
"""

from __future__ import annotations

import os
from typing import Any, Callable

import httpx
import pytest

#: 被测服务读取的配置**前缀**。按前缀清，而不是维护一份变量名清单 —— 手写清单漏过：
#: 带引擎后缀的 IMAGE_SEARCH_COOKIES_*、IMAGE_SEARCH_COOKIES_ENGINE、
#: IMAGE_SEARCH_ALLOW_ANONYMOUS / IMAGE_SEARCH_ALLOWED_ORIGINS（复核报告第 12 问）。
#: 漏一个就意味着测试行为取决于开发机上残留的变量。
CONFIG_ENV_PREFIXES = ("IMAGE_SEARCH_", "MCP_")

#: 没有前缀可循、且**不该**被采用的代理类变量：同样要清 ——
#: 否则开发机上残留一个 HTTP_PROXY，会让"环境代理不生效"这条承诺看起来成立。
BARE_PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)

#: 当前测试注册的 HTTP handler。None 表示"本测试不允许联网"。
_http_handler: Callable[[httpx.Request], httpx.Response] | None = None

_real_async_init = httpx.AsyncClient.__init__
_real_sync_init = httpx.Client.__init__


def _blocked(request: httpx.Request) -> httpx.Response:
    raise AssertionError(
        "测试发出了未经 mock 的 HTTP 请求："
        f"{request.method} {request.url}\n"
        "如果这是被测代码应有的出站请求，请用 mock_http 夹具注册 handler；"
        "如果它不该发生，说明代码有问题。"
    )


def _dispatch(request: httpx.Request) -> httpx.Response:
    if _http_handler is None:
        return _blocked(request)
    return _http_handler(request)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """把本服务的配置从环境里清干净。

    按**前缀**扫而不是查一张手写清单：清单漏过带引擎后缀的变量
    （IMAGE_SEARCH_COOKIES_TRACEMOE 之类），漏掉的变量会让测试行为取决于
    开发机上的残留。
    """
    for name in list(os.environ):
        if name.startswith(CONFIG_ENV_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    for name in BARE_PROXY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> Any:
    """给 httpx 注入 MockTransport，使未注册 handler 的请求立刻失败。"""
    global _http_handler
    _http_handler = None

    def _patched_async_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        # 测试自己传了 transport（例如要观察连接目标）就尊重它
        if "transport" not in kwargs:
            kwargs["transport"] = httpx.MockTransport(_dispatch)
        _real_async_init(self, *args, **kwargs)

    def _patched_sync_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        if "transport" not in kwargs:
            kwargs["transport"] = httpx.MockTransport(_dispatch)
        _real_sync_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_async_init)
    monkeypatch.setattr(httpx.Client, "__init__", _patched_sync_init)
    yield
    _http_handler = None


def _capture_real_getaddrinfo() -> Callable[..., Any]:
    """在**模块加载时**抓下真实的 ``socket.getaddrinfo``。

    不能等到夹具里现读：``_no_real_dns`` 是 autouse，跑到 ``allow_real_dns`` 时
    ``socket.getaddrinfo`` 早已是拦截器，现读就是"把拦截器当真函数再装回去"——
    于是"放行真实 DNS"从来没有真正放行过
    （复核报告第 12 问实测 ``allow_real_dns_restored_original False``）。
    """
    import socket

    return socket.getaddrinfo


#: 真实 resolver 的原始引用。夹具必须用它，不能用现读的 socket.getaddrinfo。
_REAL_GETADDRINFO = _capture_real_getaddrinfo()


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试里也不做真实 DNS 解析。

    "默认禁网"如果只管 HTTP，DNS 仍是漏网的一环 —— 而且解析失败会让测试
    变成依赖外网的不稳定测试。要真实解析的测试用 ``allow_real_dns`` 显式开口。
    """
    import socket

    def _blocked(*args: Any, **kwargs: Any):
        raise AssertionError(
            "测试发起了未经允许的 DNS 解析。请注入受控解析器，"
            "或用 allow_real_dns 夹具显式放行。"
        )

    monkeypatch.setattr(socket, "getaddrinfo", _blocked)


@pytest.fixture
def allow_real_dns(monkeypatch: pytest.MonkeyPatch):
    """显式放行真实 DNS（仅给需要验证真实 resolver 的测试用）。

    装回去的是模块加载时抓下的**原始函数**，不是"当前值" —— 后者是被拦截器
    覆盖过的那个。
    """
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    return _REAL_GETADDRINFO


@pytest.fixture
def real_client_init(monkeypatch: pytest.MonkeyPatch):
    """临时恢复真实的 ``httpx.AsyncClient.__init__``（不做 transport 注入）。

    为什么必须有这个开口：``_no_real_network`` 会给每个 client 注入
    ``MockTransport``，而 httpx 里算的是

        allow_env_proxies = trust_env and transport is None

    —— 一旦传了 transport，它**根本不去读环境代理**。于是"环境代理会不会被采用"
    这类行为在默认夹具下永远测不到：把实现改回 ``trust_env=True`` 测试照样全绿
    （复核报告 B6 那条假测试，根因就在这里，不在测试写法上）。

    用它时注意：这个 client 走真实 transport，出站会真的发出去。
    只构造、不发送是安全的（仓库里用它来观察 ``_mounts``）。
    """
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _real_async_init)
    yield


@pytest.fixture
def mock_http() -> Callable[[Callable[[httpx.Request], httpx.Response]], None]:
    """注册本测试的出站 HTTP handler。

    典型用法::

        def test_x(mock_http):
            seen = []
            def handler(request):
                seen.append(request)
                return httpx.Response(200, json={...})
            mock_http(handler)
    """
    def _register(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        global _http_handler
        _http_handler = handler
    return _register


@pytest.fixture
def seen_requests() -> list[httpx.Request]:
    """收集出站请求的便捷夹具，配合 mock_http 使用。"""
    return []


# --------------------------------------------------------------------------
# 常用测试数据
# --------------------------------------------------------------------------

#: 一个结构合法的最小 PNG 头（不是完整图片，够测试走到出站阶段）
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def png_base64() -> str:
    import base64
    return base64.b64encode(PNG_BYTES).decode()
