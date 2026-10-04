"""HTTP 入口与认证中间件（``main.py``）。

这一组是纯 ASGI 层的测试：不启服务、不占端口，直接把构造好的 scope 喂进去。
覆盖的是"启动期一次失败就能让整个服务裸奔"那条链的每一环。

== 被测的三个缺陷 ==

1. **静默降级**。SSE 应用构造失败时旧代码在 ``except`` 里退回
   ``mcp.http_app(path="/")`` —— Streamable HTTP 挂到了根路径上。
2. **根路径按路径放行**。旧中间件对 ``path == "/"`` 无条件放行，
   和上一条叠加就是：一次启动期异常 → 整个工具集可匿名调用。
3. **认证细节**。畸形 ``Authorization`` 头返回 500（把解析异常当服务端故障），
   比较用 ``==``，以及 WebSocket scope 掉进 HTTP 专用处理器后 ``KeyError: 'method'``。
"""

from __future__ import annotations

import json

import pytest

from image_search_mcp import main as main_mod
from image_search_mcp.main import AuthMiddleware

TOKEN = "s" * 64  # 与线上同长度


# ==========================================================================
# ASGI 驱动
# ==========================================================================

async def _call_http(app, method: str, path: str, headers=(), body: bytes = b""):
    """把一次 HTTP 请求喂给 ASGI app，返回 (status, headers, body)。"""
    raw_headers = [
        (k.lower().encode() if isinstance(k, str) else k,
         v.encode() if isinstance(v, str) else v)
        for k, v in headers
    ]
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method.upper(), "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "headers": raw_headers,
        "client": ("203.0.113.9", 51234), "server": ("127.0.0.1", 8000),
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)

    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    resp_headers = next(
        m.get("headers", []) for m in sent if m["type"] == "http.response.start"
    )
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, resp_headers, payload


async def _call_websocket(app, headers=()):
    """喂一次 WebSocket 握手，返回收到的消息序列。"""
    raw_headers = [
        (k.lower().encode() if isinstance(k, str) else k,
         v.encode() if isinstance(v, str) else v)
        for k, v in headers
    ]
    scope = {
        "type": "websocket", "asgi": {"version": "3.0"},
        "scheme": "ws", "path": "/messages/", "raw_path": b"/messages/",
        "query_string": b"", "root_path": "", "headers": raw_headers,
        "client": ("203.0.113.9", 51234), "server": ("127.0.0.1", 8000),
        "subprotocols": [],
    }
    sent: list[dict] = []
    inbox = [{"type": "websocket.connect"}]

    async def receive():
        return inbox.pop(0) if inbox else {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


class _Downstream:
    """下游桩：任何被放行的请求都返回 200 + ``{"reached": true}``。"""

    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, scope, receive, send):
        self.calls.append(dict(scope))
        body = json.dumps({"reached": True}).encode()
        await send({
            "type": "http.response.start", "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": body})


def _bearer(token: str):
    return [("authorization", f"Bearer {token}")]


# ==========================================================================
# /healthz：唯一豁免路径，且只有安全方法
# ==========================================================================

@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_healthz_is_anonymous_for_safe_methods(method: str):
    downstream = _Downstream()
    app = AuthMiddleware(downstream, TOKEN)
    status, _, _ = await _call_http(app, method, "/healthz")
    assert status == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
async def test_healthz_is_not_anonymous_for_write_methods(method: str):
    """"按路径放行"最危险的地方就是它连方法都不看。"""
    downstream = _Downstream()
    app = AuthMiddleware(downstream, TOKEN)
    status, _, _ = await _call_http(app, method, "/healthz")
    assert status == 401
    assert downstream.calls == []


async def test_root_path_is_no_longer_anonymous():
    """根路径不再是豁免路径 —— 这正是"静默降级"能得手的前提。"""
    downstream = _Downstream()
    app = AuthMiddleware(downstream, TOKEN)
    for method in ("GET", "POST"):
        status, _, _ = await _call_http(app, method, "/")
        assert status == 401, f"{method} / 不该匿名放行"
    assert downstream.calls == []


async def test_authenticated_root_still_reaches_downstream():
    """正对照：带 token 时根路径要能进去（豁免收窄了，但别把路堵死）。"""
    downstream = _Downstream()
    app = AuthMiddleware(downstream, TOKEN)
    status, _, _ = await _call_http(app, "GET", "/", _bearer(TOKEN))
    assert status == 200
    assert len(downstream.calls) == 1


# ==========================================================================
# 认证头解析
# ==========================================================================

@pytest.mark.parametrize("headers", [
    [],
    [("authorization", "Bearer " + "x" * 64)],          # 错 token
    [("authorization", "Bearer ")],
    [("authorization", TOKEN)],                          # 缺 scheme
    [("authorization", "Basic " + TOKEN)],               # 错 scheme
    [("authorization", "Bearer " + "s" * 63)],           # 少一位
    [("authorization", "Bearer " + "s" * 65)],           # 多一位
    [("authorization", "Bearer " + TOKEN[:-1] + "S")],   # 末位不同
])
async def test_bad_credentials_are_rejected_with_challenge(headers):
    downstream = _Downstream()
    app = AuthMiddleware(downstream, TOKEN)
    status, resp_headers, _ = await _call_http(app, "POST", "/messages/", headers)
    assert status == 401
    assert any(k == b"www-authenticate" for k, _ in resp_headers), (
        "401 应带 WWW-Authenticate，否则客户端不知道该用什么方式认证"
    )
    assert downstream.calls == []


@pytest.mark.parametrize("value", [
    b"\xff\xfeBearer abc",        # 非 UTF-8 字节
    b"Bearer \xc3\x28abc",        # 非法 UTF-8 序列
    b"\xff\xff\xff",
])
async def test_undecodable_header_is_400_not_500(value: bytes):
    """无法解码的头是客户端的问题（400），不是服务端故障（500）。

    旧代码在解码处直接抛异常 → 500。400 和 401 的区别在这里是实质性的：
    400 说"你这条请求本身不合法"，401 说"凭据不对，请重发一遍带对凭据的请求"——
    对一个连字节都不是合法 UTF-8 的头，让客户端重试是没有意义的。
    """
    downstream = _Downstream()
    app = AuthMiddleware(downstream, TOKEN)
    status, _, _ = await _call_http(app, "POST", "/messages/", [("authorization", value)])
    assert status == 400, f"{value!r} 应当按畸形请求处理"
    assert downstream.calls == []


@pytest.mark.parametrize("value", [
    b"Bearer",                    # 没有空格
    b"Bearer  ",                  # scheme 后只有空白
    b"Bearer \t",                 # 制表符
])
async def test_scheme_without_credentials_is_401(value: bytes):
    """头能解析、但没有凭据 —— 按"未授权"处理，不是"畸形"。

    严格说 RFC 允许对无法解析的头回 400；区别在于这条头**能**解析出一个
    已知 scheme，只是凭据是空的。让客户端按 401 走标准重试流程更合适。
    """
    downstream = _Downstream()
    app = AuthMiddleware(downstream, TOKEN)
    status, resp_headers, _ = await _call_http(app, "POST", "/messages/", [("authorization", value)])
    assert status == 401
    assert any(k == b"www-authenticate" for k, _ in resp_headers)
    assert downstream.calls == []


@pytest.mark.parametrize("value", [
    b"Bearer", b"Bearer  ", b"\xff\xfeBearer abc", b"Bearer \xc3\x28abc",
    b"", b" ", b"x" * 5000,
])
async def test_a_malformed_header_never_yields_5xx(value: bytes):
    """一条覆盖全部畸形输入的底线断言：**任何情况都不许 500**。

    500 会把"请求不合法"伪装成"服务端坏了"：监控报警指向错误的地方，
    客户端还会重试。
    """
    downstream = _Downstream()
    app = AuthMiddleware(downstream, TOKEN)
    status, _, _ = await _call_http(app, "POST", "/messages/", [("authorization", value)])
    assert 400 <= status < 500, f"{value!r} 得到了 {status}"
    assert downstream.calls == []


async def test_token_with_surrounding_whitespace_is_accepted():
    """真实 MCP 客户端和代理都可能带上多余空白 —— 这不是攻击，是常态。"""
    downstream = _Downstream()
    app = AuthMiddleware(downstream, TOKEN)
    status, _, _ = await _call_http(
        app, "POST", "/messages/", [("authorization", f"Bearer   {TOKEN}  ")]
    )
    assert status == 200


@pytest.mark.parametrize(
    "headers",
    [
        # 有效在前、无效在后：旧实现的 dict() 取后者 → 恰好 401
        # （原来的测试只覆盖了这一种，于是"重复必须拒绝"这句是没被验证的）
        [("authorization", f"Bearer {TOKEN}"), ("authorization", "Bearer " + "x" * 64)],
        # 无效在前、有效在后：旧实现取到了有效值 → 实测 204 放行，这就是漏掉的一半
        [("authorization", "Bearer " + "x" * 64), ("authorization", f"Bearer {TOKEN}")],
        # 两个都有效：同样是歧义，照样拒绝（不能因为"值都对"就放行）
        [("authorization", f"Bearer {TOKEN}"), ("authorization", f"Bearer {TOKEN}")],
    ],
)
async def test_duplicate_authorization_headers_fail_closed(headers):
    """重复的认证头必须拒绝，不能"取第一个/最后一个"。

    代理链上出现重复头时，两端的解读不一致就是经典的绕过手法。旧实现直接
    ``dict(scope["headers"])``（后者覆盖前者），于是**头顺序决定**是否放行。
    重复头是歧义请求，回 400（与"畸形头回 400"的既有约定一致），
    而不是伪装成 401 让人以为只是 token 不对。
    """
    downstream = _Downstream()
    app = AuthMiddleware(downstream, TOKEN)
    status, _, _ = await _call_http(app, "POST", "/messages/", headers)
    assert status == 400
    assert downstream.calls == []


async def test_duplicate_origin_headers_are_also_rejected():
    """Origin 也参与安全判定（来源白名单），重复同样是歧义。

    这条同时证明重复检查发生在"来源是否允许"之前：否则这里只会得到 403。
    """
    downstream = _Downstream()
    app = AuthMiddleware(downstream, TOKEN)
    status, _, _ = await _call_http(
        app,
        "POST",
        "/messages/",
        [
            ("authorization", f"Bearer {TOKEN}"),
            ("origin", "https://example.com"),
            ("origin", "https://evil.example"),
        ],
    )
    assert status == 400
    assert downstream.calls == []


def test_comparison_uses_compare_digest():
    """源码级确认用的是定时安全比较。

    ``==`` 的比较耗时与"前多少位相同"相关，逐字节爆破 token 在理论上可行。
    这里不做计时测量（噪声太大、结论不可靠），直接确认实现选择。
    """
    import inspect
    src = inspect.getsource(main_mod.AuthMiddleware._check_authorization)
    assert "compare_digest" in src, "凭据比较应当使用 secrets.compare_digest"
    assert "== expected" not in src and "== token" not in src


# ==========================================================================
# Origin（DNS rebinding 防御）
# ==========================================================================

async def test_browser_origin_not_in_allowlist_is_rejected():
    downstream = _Downstream()
    app = AuthMiddleware(downstream, TOKEN, allowed_origins=frozenset())
    status, _, _ = await _call_http(
        app, "POST", "/messages/",
        _bearer(TOKEN) + [("origin", "https://evil.example")],
    )
    assert status == 403
    assert downstream.calls == []


async def test_configured_origin_is_allowed():
    downstream = _Downstream()
    app = AuthMiddleware(
        downstream, TOKEN, allowed_origins=frozenset({"https://good.example"})
    )
    status, _, _ = await _call_http(
        app, "POST", "/messages/",
        _bearer(TOKEN) + [("origin", "https://good.example")],
    )
    assert status == 200


async def test_native_client_without_origin_is_allowed():
    """无 Origin 头是原生 MCP 客户端的正常形态 —— 不能因为防浏览器就把它们挡住。"""
    downstream = _Downstream()
    app = AuthMiddleware(downstream, TOKEN, allowed_origins=frozenset())
    status, _, _ = await _call_http(app, "POST", "/messages/", _bearer(TOKEN))
    assert status == 200


def test_origins_are_parsed_from_env():
    assert main_mod._parse_origins(None) == frozenset()
    assert main_mod._parse_origins("") == frozenset()
    assert main_mod._parse_origins(" https://a.example , https://b.example ") == (
        frozenset({"https://a.example", "https://b.example"})
    )


# ==========================================================================
# WebSocket
# ==========================================================================

async def test_websocket_without_token_is_closed_not_crashed():
    """旧代码把 WebSocket scope 交给只认 ``scope["method"]`` 的处理器，
    结果是 ``KeyError: 'method'`` —— 既是健壮性 bug，也让连接状态不明。

    正确做法是明确握手拒绝（1008 = policy violation）。
    """
    downstream = _Downstream()
    app = AuthMiddleware(downstream, TOKEN)
    sent = await _call_websocket(app, [])
    assert sent, "应当给出明确回应，而不是静默失败"
    assert sent[0]["type"] == "websocket.close"
    assert sent[0].get("code") == 1008
    assert downstream.calls == []


async def test_websocket_with_token_is_also_refused():
    """即便带了 token 也拒绝 —— 这个服务不需要 WebSocket 通道，
    "少一条协议"比"多一条要维护的认证分支"更安全。"""
    downstream = _Downstream()
    app = AuthMiddleware(downstream, TOKEN)
    sent = await _call_websocket(app, _bearer(TOKEN))
    assert sent[0]["type"] == "websocket.close"
    assert downstream.calls == []


async def test_lifespan_scope_passes_through():
    """lifespan 不是请求，不该被认证拦下（否则服务起不来）。"""
    downstream = _Downstream()
    app = AuthMiddleware(downstream, TOKEN)
    sent: list[dict] = []

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        sent.append(message)

    await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)

    assert downstream.calls, "lifespan 应当被放行到达下游"
    assert downstream.calls[0]["type"] == "lifespan"
    assert not any(m.get("type") == "websocket.close" for m in sent)


# ==========================================================================
# 启动闸门
# ==========================================================================

@pytest.fixture
def no_server(monkeypatch):
    """任何测试里都不许真的把 uvicorn 起起来。

    起因是一次反向测试：注入了"缺 token 也继续跑"之后，
    ``test_missing_token_refuses_to_start`` 不是断言失败，而是**挂住 30 秒**
    然后抛一屏超时堆栈 —— 因为 ``main()`` 一路走到了 ``uvicorn.run`` 并阻塞。
    这种失败在 CI 上表现为"卡住"，比失败难查得多，而且它掩盖了真正的原因。

    装一个会响的桩：一旦走到起服务这一步，就带着说明直接失败。
    """
    import uvicorn

    calls: list[tuple] = []

    def _run(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError(
            "main() 走到了 uvicorn.run —— 期望它在启动之前就拒绝（或测试桩已生效）"
        )

    monkeypatch.setattr(uvicorn, "run", _run)
    return calls


def test_missing_token_refuses_to_start(monkeypatch, no_server):
    """没有 token 就不许启动 —— 旧代码此时会起一个任何人都能调的服务。"""
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        main_mod.main(["--sse", "--host", "0.0.0.0", "--port", "8000"])
    assert no_server == [], "拒绝启动不该走到起服务那一步"


def test_blank_token_counts_as_missing(monkeypatch, no_server):
    """只有空白的 token 等于没有 token —— 不 strip 的话它就是一个"能用"的凭据。"""
    monkeypatch.setenv("MCP_AUTH_TOKEN", "   ")
    with pytest.raises(SystemExit):
        main_mod.main(["--sse", "--host", "0.0.0.0"])


def test_anonymous_mode_is_refused_on_a_public_interface(monkeypatch, no_server):
    """``IMAGE_SEARCH_ALLOW_ANONYMOUS`` 只对回环地址有效 —— 匿名服务不许对公网开。"""
    monkeypatch.setenv(main_mod.ALLOW_ANONYMOUS_VAR, "1")
    with pytest.raises(SystemExit):
        main_mod.main(["--sse", "--host", "0.0.0.0"])


def test_anonymous_flag_alone_is_not_enough_on_a_public_interface(monkeypatch, no_server):
    """把开关放在命令行也绕不过去 —— 两条入口走同一个校验。"""
    monkeypatch.delenv(main_mod.ALLOW_ANONYMOUS_VAR, raising=False)
    with pytest.raises(SystemExit):
        main_mod.main(["--sse", "--allow-anonymous", "--host", "0.0.0.0"])


def test_anonymous_mode_is_allowed_on_loopback(monkeypatch):
    monkeypatch.setenv(main_mod.ALLOW_ANONYMOUS_VAR, "1")
    token = main_mod._resolve_token_and_mode("127.0.0.1", allow_anonymous=True)
    assert token is None  # None 表示"刻意不认证"


def test_valid_token_is_returned_stripped(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", f"  {TOKEN}  ")
    assert main_mod._resolve_token_and_mode("0.0.0.0", allow_anonymous=False) == TOKEN


def test_short_token_warns_but_still_starts(monkeypatch, capsys):
    """短 token 只警告不拒绝 —— 拒绝会让既有部署直接起不来，
    而"起不来"往往被运维用更差的办法绕过。"""
    monkeypatch.setenv("MCP_AUTH_TOKEN", "short")
    assert main_mod._resolve_token_and_mode("0.0.0.0", allow_anonymous=False) == "short"
    assert "建议至少 16" in capsys.readouterr().err


def test_two_requests_do_not_share_state():
    """中间件不能把上次的结果记在实例上（否则一次通过就永久通过）。

    用同一实例连续发两次请求：第一次带对 token、第二次不带。
    如果校验结果被缓存/复用，第二次会被误放行。
    """
    import asyncio

    downstream = _Downstream()
    app = AuthMiddleware(downstream, TOKEN)

    async def scenario():
        ok, _, _ = await _call_http(app, "POST", "/messages/", _bearer(TOKEN))
        bad, _, _ = await _call_http(app, "POST", "/messages/", [])
        return ok, bad

    assert asyncio.run(scenario()) == (200, 401)


# ==========================================================================
# 静默降级
# ==========================================================================

def _route_paths(app) -> list[str]:
    return [
        getattr(route, "path", "")
        for route in getattr(app, "routes", [])
        if getattr(route, "path", None)
    ]


def test_protocol_endpoint_is_not_mounted_at_root():
    """协议端点必须在自己的路径上，不能挂在根路径。

    直接看**构造出来的路由表**，而不是看调用参数 —— 参数语义会变
    （实测：对 ``transport="sse"``，``path`` 控制的是 SSE 端点本身，
    传 ``path="/messages"`` 会让两个路由撞在一起），路由表不会骗人。

    这条同时防的是：有人为了"顺手修一下"把路径改成 ``/``，
    于是协议端点落回中间件的豁免面之外又被无条件放行。
    """
    paths = _route_paths(main_mod.build_sse_app())
    assert "/" not in paths, f"协议端点不该挂在根路径：{paths}"
    assert "/sse" in paths, f"SSE 端点应当钉在 /sse：{paths}"
    assert paths.count("/messages") == 1, f"消息路径不该重复：{paths}"


def test_root_path_is_only_the_homepage():
    """``/`` 上只允许挂那个需要认证的说明页。"""
    from starlette.routing import Route

    app = main_mod.build_sse_app()
    main_mod._install_routes(app, version="0.0.0-test")
    root_routes = [r for r in app.routes if getattr(r, "path", None) == "/"]
    assert len(root_routes) == 1
    assert isinstance(root_routes[0], Route)
    assert root_routes[0].endpoint.__name__ == "homepage"


def test_build_sse_app_does_not_fall_back(monkeypatch):
    """构造失败必须抛出，而不是换一个路径挂上去继续跑。"""
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(main_mod.mcp, "http_app", _boom)
    with pytest.raises(RuntimeError):
        main_mod.build_sse_app()


def test_build_sse_app_succeeds_normally():
    """正对照：正常路径要能构造出应用。"""
    app = main_mod.build_sse_app()
    assert app is not None
