"""MCP 服务的 HTTP 入口与认证中间件。

== 与本模块相关的历史缺陷 ==

1. **静默降级**。旧代码在 SSE 应用构造失败时，``except`` 里退回
   ``mcp.http_app(path="/")`` —— 也就是把 Streamable HTTP 挂到根路径上，
   而根路径恰好被认证中间件无条件放行。两者叠加的结果：启动期一次失败
   就能让整个服务在根路径上匿名可用。

2. **根路径豁免不分方法**。``if scope["path"] == "/"`` 直接放行，
   既不检查方法也不检查是哪条路由；而 ``Route("/")`` 只处理 GET/HEAD，
   POST 会继续往下面的 MCP 路由匹配。

3. **缺 token 时照常启动**。没有 token 就不加认证中间件，
   服务以 ``--host 0.0.0.0`` 起来且完全开放，日志里只有一行"启动成功"。

4. **认证细节**：用 ``==`` 比较 token；畸形 Authorization 头
   （含非法 UTF-8 字节）会在 ``.decode()`` 处抛异常变成 HTTP 500；
   不校验 Origin；WebSocket 直接穿过中间件掉进 HTTP 专用的处理器
   （实测抛 ``KeyError: 'method'``，拿不到数据，但表现为服务端错误）。

现在的姿态是**安全默认**：没有有效 token 就拒绝启动；豁免只给一个只读的
``/healthz``；异常一律变成明确的状态码而不是 500。
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import secrets
import sys

from .server import mcp

#: 唯一免认证路径。只读、不访问任何引擎（见 /healthz 实现）。
HEALTH_PATH = "/healthz"

#: 只在开发模式允许匿名访问时使用的开关
ALLOW_ANONYMOUS_VAR = "IMAGE_SEARCH_ALLOW_ANONYMOUS"
#: 允许的浏览器 Origin（逗号分隔）。不设置时：任何带 Origin 的请求都被拒。
ALLOWED_ORIGINS_VAR = "IMAGE_SEARCH_ALLOWED_ORIGINS"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
SAFE_METHODS = frozenset({"GET", "HEAD"})


def _is_loopback(host: str) -> bool:
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _parse_origins(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(part.strip().rstrip("/") for part in raw.split(",") if part.strip())


class AuthMiddleware:
    """Bearer Token 认证。

    放行规则（白名单式）：

    - ``lifespan`` 与其它非 http/websocket 的 scope 直接透传
    - **websocket 明确拒绝**（1008）：不认证不拒绝的话，它会掉进 HTTP 专用的
      处理器里抛 ``KeyError``，表现为服务端 500 一类的东西
    - ``GET``/``HEAD`` 的 ``/healthz`` 免认证
    - 其余一律要求 ``Authorization: Bearer <token>``
    - 带 ``Origin`` 的请求，Origin 必须在允许列表里（原生客户端不发 Origin，照常放行）
    """

    def __init__(
        self,
        app,
        token: str,
        *,
        allowed_origins: frozenset[str] = frozenset(),
    ):
        self.app = app
        self._token = token.strip().encode("utf-8")
        self._allowed_origins = allowed_origins

    async def __call__(self, scope, receive, send):
        scope_type = scope.get("type")

        if scope_type == "websocket":
            await self._reject_websocket(receive, send)
            return

        if scope_type != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = (scope.get("method") or "").upper()

        # 豁免收窄成一个只读端点，且只对安全方法生效
        if path == HEALTH_PATH and method in SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))

        rejection = self._check_origin(headers)
        if rejection is not None:
            await self._send_plain(send, 403, rejection)
            return

        ok, error = self._check_authorization(headers)
        if error is not None:
            # 畸形头是调用方的问题，不是服务端错误 —— 回 400 而不是 500
            await self._send_plain(send, 400, error)
            return
        if not ok:
            await self._send_plain(send, 401, "Unauthorized: Invalid or missing Bearer token")
            return

        await self.app(scope, receive, send)

    # -- 各项检查 ---------------------------------------------------------

    def _check_origin(self, headers: dict[bytes, bytes]) -> str | None:
        raw = headers.get(b"origin")
        if raw is None:
            return None  # 原生 MCP 客户端不发 Origin
        try:
            origin = raw.decode("utf-8").rstrip("/")
        except UnicodeDecodeError:
            return "Origin header is not valid UTF-8"
        if not self._allowed_origins:
            return (
                "Origin not allowed: this server rejects browser origins unless "
                f"{ALLOWED_ORIGINS_VAR} lists them"
            )
        if origin not in self._allowed_origins:
            return "Origin not allowed"
        return None

    def _check_authorization(self, headers: dict[bytes, bytes]) -> tuple[bool, str | None]:
        """返回 (是否通过, 错误信息)。错误信息非 None 表示畸形输入。"""
        raw = headers.get(b"authorization")
        if raw is None:
            return False, None
        try:
            auth_header = raw.decode("utf-8")
        except UnicodeDecodeError:
            return False, "Authorization header is not valid UTF-8"

        parts = auth_header.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False, None

        candidate = parts[1].strip().encode("utf-8")
        # 常量时间比较：避免用 == 泄露前缀匹配长度
        if secrets.compare_digest(candidate, self._token):
            return True, None
        return False, None

    # -- 响应 -------------------------------------------------------------

    async def _send_plain(self, send, status: int, body: str) -> None:
        headers = [(b"content-type", b"text/plain; charset=utf-8")]
        if status == 401:
            headers.append((b"www-authenticate", b'Bearer realm="image-search-mcp"'))
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": headers,
        })
        await send({
            "type": "http.response.body",
            "body": body.encode("utf-8"),
        })

    async def _reject_websocket(self, receive, send) -> None:
        try:
            message = await receive()
            if message.get("type") == "websocket.connect":
                await send({"type": "websocket.close", "code": 1008})
        except Exception:  # noqa: BLE001 - 关闭过程中出错不值得让连接挂住
            pass


# --------------------------------------------------------------------------
# 应用装配
# --------------------------------------------------------------------------

def build_sse_app():
    """构造 SSE 应用。

    **没有降级分支** —— 构造失败就让它抛出去，由 ``main()`` 终止进程。
    旧代码在这里 ``except`` 后改用 ``http_app(path="/")``，而根路径是中间件的
    无条件豁免项，于是"构造失败"变成了"匿名可用"。一个协议替换不该由异常驱动。
    """
    if hasattr(mcp, "http_app"):
        # 显式钉住路径，不吃默认值。
        #
        # 这里有个实测出来的坑：对 ``transport="sse"``，``path`` 控制的是
        # **SSE 端点本身**，而消息端点是固定 `/messages`。所以传
        # ``path="/messages"`` 会让两者撞在同一个路径上（实测路由变成
        # ``['/messages', '/messages']``）。正确值是 ``/sse``。
        #
        # 更要紧的是 ``path="/"`` 也合法 —— 一旦有人这么写，协议端点就挂到
        # 根路径上，和中间件里"根路径放行"的老豁免叠加，等于匿名开放。
        return mcp.http_app(transport="sse", path="/sse")
    from mcp.server.fastmcp import create_sse_app  # pragma: no cover - 旧版兼容
    return create_sse_app(mcp, sse_path="/sse", message_path="/messages")


def _install_routes(app, *, version: str) -> None:
    """挂上 ``/healthz``（免认证）与 ``/``（需要认证）。"""
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def healthz(request):
        # 刻意**不**访问任何引擎：真实搜图会烧配额，还会让上游抽风触发本机重启。
        # 这里只回答"进程活着、应用装起来了"。
        return JSONResponse({
            "status": "ok",
            "service": "image-search-mcp",
            "version": version,
        })

    async def homepage(request):
        return JSONResponse({
            "service": "Image Search MCP Server",
            "endpoints": {"sse": "/sse", "messages": "/messages", "health": HEALTH_PATH},
        })

    if not hasattr(app, "routes"):
        return
    app.routes.insert(0, Route(HEALTH_PATH, healthz, methods=["GET", "HEAD"]))
    app.routes.append(Route("/", homepage, methods=["GET", "HEAD"]))


def _resolve_token_and_mode(host: str, *, allow_anonymous: bool) -> str | None:
    """校验启动姿态。返回 token（None 表示显式匿名开发模式）。

    不安全就 ``SystemExit``，**不是**"继续启动但没有任何认证"。
    """
    token = (os.environ.get("MCP_AUTH_TOKEN") or "").strip()

    if allow_anonymous:
        if not _is_loopback(host):
            raise SystemExit(
                f"拒绝启动：显式匿名模式（{ALLOW_ANONYMOUS_VAR} / --allow-anonymous）"
                f"只允许绑定回环地址，当前 --host={host}。"
            )
        print("警告：匿名模式已开启，且仅绑定回环地址。不要对外暴露。", file=sys.stderr)
        return None

    if not token:
        raise SystemExit(
            "拒绝启动：HTTP 模式需要 MCP_AUTH_TOKEN，但环境变量为空。\n"
            f"  要么设置 MCP_AUTH_TOKEN=<足够长的随机串>，\n"
            f"  要么显式开启本地开发模式：{ALLOW_ANONYMOUS_VAR}=1 并绑定 --host 127.0.0.1。"
        )

    if len(token) < 16:
        print(
            f"警告：MCP_AUTH_TOKEN 只有 {len(token)} 个字符，建议至少 16 个。",
            file=sys.stderr,
        )

    return token


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Image Search MCP Server")
    parser.add_argument("--sse", action="store_true", help="Run in SSE mode (HTTP)")
    parser.add_argument("--port", type=int, default=8000, help="Port for SSE mode")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host for SSE mode")
    parser.add_argument(
        "--allow-anonymous",
        action="store_true",
        help=f"Explicitly allow anonymous HTTP access (loopback only). Same as {ALLOW_ANONYMOUS_VAR}=1.",
    )
    args = parser.parse_args(argv)

    if not args.sse:
        # stdio 模式由调用方（本机进程）拥有管道，不需要 HTTP token
        mcp.run()
        return

    import uvicorn

    from . import __version__

    # 先定启动姿态，再碰应用装配 —— 免得构造失败时先打出误导性的日志
    anonymous = args.allow_anonymous or os.environ.get(ALLOW_ANONYMOUS_VAR) == "1"
    token = _resolve_token_and_mode(args.host, allow_anonymous=anonymous)

    # 构造失败就抛出去，由 __main__ / 调用方看到非零退出码
    app = build_sse_app()
    _install_routes(app, version=__version__)

    if token is not None:
        origins = _parse_origins(os.environ.get(ALLOWED_ORIGINS_VAR))
        app = AuthMiddleware(app, token, allowed_origins=origins)
        print("Authentication enabled (Bearer token required; anonymous exempt: "
              f"{HEALTH_PATH} GET/HEAD).")
        if not origins:
            print(f"Note: no {ALLOWED_ORIGINS_VAR} configured — requests carrying an "
                  "Origin header are rejected.")
    else:
        print("WARNING: running without authentication on loopback only.")

    print(f"Starting SSE server on {args.host}:{args.port}...")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
