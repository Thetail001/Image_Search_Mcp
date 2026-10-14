"""下载由本机抓取的用户图片。

拒绝非公网地址；连接固定到已校验的 IP，同时保留原主机的 Host 与 TLS SNI。
逐跳校验重定向，限制响应大小与总时间，不使用代理或引擎凭据。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import httpx

#: 域名解析器：``(host, port) -> 地址字符串列表``。可注入，便于测试观察
#: 连接层实际收到的目的地址（只断言 URL 字符串是测不到 DNS 这一步的）。
Resolver = Callable[[str, int], Awaitable[list[str]]]

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class DownloadError(Exception):
    """下载失败。``reason`` 是**可判定**的类别，供上层做决策（例如是否值得兜底重试）。"""

    def __init__(self, reason: str, message: str, *, url: str = ""):
        self.reason = reason
        self.url = url
        super().__init__(message)


@dataclass(frozen=True)
class DownloadPolicy:
    """下载预算。每一项都有明确数值与单位，便于判定。"""

    #: 解码后的最大字节数（``Content-Encoding`` 已解开 —— 按解压后的量限，
    #: 否则压缩炸弹可以绕过）
    max_bytes: int = 8 * 1024 * 1024
    #: 最大重定向跳数（不含首次请求）
    max_redirects: int = 3
    #: 总期限：覆盖 DNS、连接、重定向、读取全部阶段
    total_timeout: float = 15.0
    #: 单次连接超时
    connect_timeout: float = 5.0
    #: 允许的协议
    allowed_schemes: frozenset[str] = field(
        default_factory=lambda: frozenset({"http", "https"})
    )
    #: 是否允许 https 降级到 http
    allow_https_downgrade: bool = False
    #: 是否允许目标带用户名密码（http://user:pass@host/）—— 默认不允许
    allow_userinfo: bool = False
    user_agent: str = "image-search-mcp/0.2 (safe-downloader)"

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("max_bytes 必须为正")
        if self.max_redirects < 0:
            raise ValueError("max_redirects 不能为负")
        if self.total_timeout <= 0:
            raise ValueError("total_timeout 必须为正")


# --------------------------------------------------------------------------
# 地址判定
# --------------------------------------------------------------------------

def _unwrap_ipv4_mapped(addr: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """IPv4-mapped IPv6（::ffff:127.0.0.1）必须按 IPv4 判定，否则会绕过内网检查。"""
    if addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    # ::ffff:0:0/96 之外的兼容形式也一并处理
    if addr.sixtofour is not None:
        return addr.sixtofour
    return None


#: 显式拒绝 stdlib 地址标志在不同 Python 版本中可能漏判的特殊网段。
_EXTRA_DENIED_NETWORKS: tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network, ...
] = (
    ipaddress.ip_network("fec0::/10"),       # 已废弃的站点本地（RFC 3879）
    ipaddress.ip_network("192.88.99.0/24"),  # 已废弃的 6to4 中继任播（RFC 7526）
)


def is_public_address(raw: str) -> bool:
    """判断地址是否为可访问的公网 IPv4 或 IPv6。映射地址按其 IPv4 地址检查。"""
    try:
        addr = ipaddress.ip_address(raw.split("%")[0])  # 去 zone id
    except ValueError:
        return False

    if isinstance(addr, ipaddress.IPv6Address):
        mapped = _unwrap_ipv4_mapped(addr)
        if mapped is not None:
            return is_public_address(str(mapped))

    # 跨版本稳定的显式拒绝，放在 flag 之前判
    if any(addr in net for net in _EXTRA_DENIED_NETWORKS):
        return False

    # is_global 在部分 Python 版本对某些保留段判定不一致，逐项显式判否更稳
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return False

    return addr.is_global


# --------------------------------------------------------------------------
# URL 校验
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int
    path_and_query: str

    @property
    def _authority(self) -> str:
        """生成 URL authority；IPv6 字面量保留方括号。"""
        host = f"[{self.host}]" if ":" in self.host else self.host
        default = 443 if self.scheme == "https" else 80
        return host if self.port == default else f"{host}:{self.port}"

    @property
    def host_header(self) -> str:
        return self._authority

    @property
    def display(self) -> str:
        return f"{self.scheme}://{self._authority}{self.path_and_query}"


def parse_target(url: str, policy: DownloadPolicy) -> Target:
    """解析并规范化 URL，将无效协议、主机和端口归为 invalid_url。"""
    if not isinstance(url, str) or not url.strip():
        raise DownloadError("invalid_url", "URL 为空", url=str(url))

    try:
        parts = urlsplit(url.strip())
        scheme = (parts.scheme or "").lower()
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise DownloadError("invalid_url", f"URL 无法解析：{exc}", url=url) from exc

    if scheme not in policy.allowed_schemes:
        raise DownloadError(
            "invalid_url",
            f"不支持的协议 {scheme!r}（允许 {sorted(policy.allowed_schemes)}）",
            url=url,
        )

    if not hostname:
        raise DownloadError("invalid_url", f"URL 缺少主机名：{url!r}", url=url)

    if (parts.username or parts.password) and not policy.allow_userinfo:
        raise DownloadError("invalid_url", "URL 不允许内嵌用户名/密码", url=url)

    if port is None:
        port = 443 if scheme == "https" else 80
    if not (1 <= port <= 65535):
        raise DownloadError("invalid_url", f"端口 {port} 非法", url=url)

    # 非 ASCII 主机名先转 IDNA。不转的话它会被原样放进 Host 头，
    # 直到构造请求时才抛出 UnicodeEncodeError。
    bare_host = hostname.rstrip(".")
    try:
        ipaddress.ip_address(bare_host.split("%")[0])  # 字面量地址：原样保留
        host = bare_host
    except ValueError:
        try:
            host = bare_host.encode("idna").decode("ascii")
        except (UnicodeError, ValueError) as exc:
            raise DownloadError(
                "invalid_url", f"主机名无法转为 IDNA：{exc}", url=url
            ) from exc

    if not host:
        raise DownloadError("invalid_url", "URL 主机名为空", url=url)

    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    return Target(scheme=scheme, host=host, port=port, path_and_query=path)


# --------------------------------------------------------------------------
# DNS
# --------------------------------------------------------------------------

async def default_resolver(host: str, port: int) -> list[str]:
    """解析出全部地址（IPv4 与 IPv6）。"""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in seen:
            seen.append(addr)
    return seen


async def resolve_and_validate(
    target: Target,
    policy: DownloadPolicy,
    resolver: Resolver,
) -> list[str]:
    """解析目标地址；任意一条结果为非公网地址时拒绝整个请求。"""
    # 字面量地址不需要解析
    try:
        ipaddress.ip_address(target.host.split("%")[0])
        candidates = [target.host]
    except ValueError:
        try:
            candidates = list(await resolver(target.host, target.port))
        except DownloadError:
            raise
        except Exception as exc:  # noqa: BLE001 - 解析失败必须拒绝，不能退回未校验连接
            raise DownloadError(
                "dns_failure", f"域名解析失败：{target.host}（{exc}）", url=target.display
            ) from exc

    if not candidates:
        raise DownloadError("dns_failure", f"域名无解析结果：{target.host}", url=target.display)

    blocked = [addr for addr in candidates if not is_public_address(addr)]
    if blocked:
        raise DownloadError(
            "blocked_address",
            f"目标解析出非公网地址，拒绝访问：{target.host} -> {blocked}",
            url=target.display,
        )

    return candidates


# --------------------------------------------------------------------------
# 连接
# --------------------------------------------------------------------------

def _pinned_url(target: Target, address: str) -> str:
    host = f"[{address}]" if ":" in address else address
    default = 443 if target.scheme == "https" else 80
    netloc = host if target.port == default else f"{host}:{target.port}"
    return f"{target.scheme}://{netloc}{target.path_and_query}"


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

class SafeDownloader:
    """具备地址校验、重定向控制、体积限制和总期限的图片下载器。"""

    def __init__(
        self,
        policy: DownloadPolicy | None = None,
        *,
        resolver: Resolver | None = None,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        proxy: str | None = None,
    ):
        self.policy = policy or DownloadPolicy()
        self._resolver = resolver or default_resolver
        self._transport_factory = transport_factory
        if proxy:
            # 代理替本机解析目标域名 ⇒ 本机 DNS 校验无法证明最终连到哪里。
            # 契约里的处理方式是"暂不支持的组合直接拒绝"，而不是默默放行。
            raise DownloadError(
                "unsupported_proxy",
                "用户图源下载不接受代理：代理会代替本机解析域名，"
                "DNS 校验将无法证明实际连接目标",
            )
        self._proxy = None

    async def fetch(self, url: str) -> bytes:
        """在总期限内下载图片，覆盖 DNS、连接、重定向和读取。

        asyncio.wait_for 可在 Python 3.10 上使用，并能取消阻塞的 await。"""
        budget = self.policy.total_timeout
        try:
            return await asyncio.wait_for(self._fetch_hops(url, budget), budget)
        except asyncio.TimeoutError as exc:
            raise DownloadError("timeout", "下载总期限已到", url=url) from exc

    async def _fetch_hops(self, url: str, budget: float) -> bytes:
        deadline = time.monotonic() + budget
        current = url
        visited: list[str] = []

        for hop in range(self.policy.max_redirects + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DownloadError("timeout", "下载总期限已到", url=current)

            target = parse_target(current, self.policy)
            # 把当前这一跳记进 visited。不记的话，一个指回起点的重定向
            # 要等到再发一次请求之后才被发现。
            if target.display not in visited:
                visited.append(target.display)
            addresses = await resolve_and_validate(target, self.policy, self._resolver)

            response_status, location, body = await self._request_once(
                target, addresses, deadline
            )

            if response_status in REDIRECT_STATUSES:
                # 缺 Location、成环要先判：放在跳数之后的话，在最后一跳
                # 这些更具体的原因会被 too_many_redirects 盖住
                if not location:
                    raise DownloadError("redirect_without_location", "重定向缺少 Location", url=current)

                # 每跳都重新解析、重新校验 —— 不复用上一跳的判定
                next_target = parse_target(urljoin(current, location), self.policy)
                if (
                    target.scheme == "https"
                    and next_target.scheme == "http"
                    and not self.policy.allow_https_downgrade
                ):
                    raise DownloadError(
                        "https_downgrade",
                        "重定向试图把 https 降级到 http，拒绝",
                        url=next_target.display,
                    )
                if next_target.display in visited:
                    raise DownloadError("redirect_loop", "重定向成环", url=next_target.display)
                if hop >= self.policy.max_redirects:
                    raise DownloadError(
                        "too_many_redirects",
                        f"重定向超过 {self.policy.max_redirects} 跳",
                        url=current,
                    )
                current = next_target.display
                continue

            if not body:
                raise DownloadError("empty_body", "目标返回空内容", url=target.display)

            return body

        raise DownloadError("too_many_redirects", "重定向次数超限", url=url)

    async def _request_once(
        self,
        target: Target,
        addresses: list[str],
        deadline: float,
    ) -> tuple[int, str | None, bytes]:
        """依次尝试已校验的地址，不自动跟随重定向。"""
        last_error: Exception | None = None
        timed_out = False

        for address in addresses:
            try:
                return await self._request_pinned(target, address, deadline)
            except DownloadError:
                raise
            except httpx.TimeoutException as exc:
                # 单次 I/O 超时与"连不上"对排查的含义完全不同：
                # 过去 ReadTimeout 也被归成 connection_failed
                last_error = exc
                timed_out = True
                continue
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc
                continue

        # 只报异常类型名，不把 str(exc) 拼进来：它可能带目标 URL 或本地细节
        raise DownloadError(
            "timeout" if timed_out else "connection_failed",
            f"无法连接 {target.host}（尝试 {addresses}）："
            f"{type(last_error).__name__ if last_error else '未知异常'}",
            url=target.display,
        )

    async def _request_pinned(
        self,
        target: Target,
        address: str,
        deadline: float,
    ) -> tuple[int, str | None, bytes]:
        # 连到已校验的那个 IP；Host 与 TLS SNI 仍用原主机名，证书按原主机名校验
        pinned = _pinned_url(target, address)
        headers = {
            "Host": target.host_header,
            "User-Agent": self.policy.user_agent,
            "Accept": "*/*",
            # 明确要未压缩的字节。图片本身已是压缩格式，再叠一层内容编码
            # 只会把"先解压再计数"变成一条内存放大路径（见 _read_bounded 的说明）。
            "Accept-Encoding": "identity",
        }
        extensions = {}
        if target.scheme == "https":
            extensions["sni_hostname"] = target.host

        remaining = max(deadline - time.monotonic(), 0.001)
        kwargs: dict = {
            "follow_redirects": False,
            "trust_env": False,          # 不继承 HTTP_PROXY 之类的环境代理
            "proxy": self._proxy,
            "timeout": httpx.Timeout(
                remaining, connect=min(self.policy.connect_timeout, remaining)
            ),
            "verify": True,
        }
        if self._transport_factory is not None:
            kwargs["transport"] = self._transport_factory()

        client = httpx.AsyncClient(**kwargs)
        try:
            async with client.stream(
                "GET", pinned, headers=headers, extensions=extensions
            ) as response:
                status = response.status_code
                if status in REDIRECT_STATUSES:
                    return status, response.headers.get("location"), b""

                if status >= 400:
                    # 先处理 HTTP 错误，避免下载错误页面的 body。
                    raise DownloadError(
                        "http_error", f"目标返回 HTTP {status}", url=target.display
                    )

                # Content-Length 只用来提前拒绝，不能代替实际计数：
                # 分块响应、以及谎报长度的响应都必须靠流式累计来兜住
                declared = response.headers.get("content-length")
                if declared is not None:
                    try:
                        if int(declared) > self.policy.max_bytes:
                            raise DownloadError(
                                "too_large",
                                f"Content-Length {declared} 超过上限 {self.policy.max_bytes} 字节",
                                url=target.display,
                            )
                    except ValueError:
                        pass

                encoding = (response.headers.get("content-encoding") or "").strip().lower()
                if encoding and encoding != "identity":
                    # 在读取前拒绝压缩响应：httpx 的解压先于流式计数，可能放大内存占用。
                    raise DownloadError(
                        "unsupported_content_encoding",
                        f"目标以 Content-Encoding: {encoding} 压缩响应，"
                        f"拒绝在计数之前解压",
                        url=target.display,
                    )

                body = await self._read_bounded(response, target, deadline)
                return status, None, body
        finally:
            # 超限、超时、取消都要关掉 client —— 不留挂起连接
            await client.aclose()

    async def _read_bounded(
        self,
        response: httpx.Response,
        target: Target,
        deadline: float,
    ) -> bytes:
        """流式累计响应字节数，并在每个数据块到达时检查总期限。

        外层 wait_for 处理阻塞等待；循环内检查处理持续产出但不让出执行权的流。
        非 identity 内容编码已在读取前拒绝，避免先解压后计数造成内存放大。"""
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            if time.monotonic() > deadline:
                raise DownloadError("timeout", "下载总期限已到", url=target.display)
            total += len(chunk)
            if total > self.policy.max_bytes:
                # 立刻停手：不再 aiter 下去，让连接被关掉
                raise DownloadError(
                    "too_large",
                    f"响应超过上限 {self.policy.max_bytes} 字节（已读取 {total}）",
                    url=target.display,
                )
            chunks.append(chunk)
        return b"".join(chunks)


#: 使用本机安全下载器的引擎；该集合由上游源码检查验证。
ENGINES_THAT_FETCH_LOCALLY: frozenset[str] = frozenset({"EHentai", "BaiDu"})
