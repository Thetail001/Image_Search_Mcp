"""取图下载器：把"服务器替用户去下载一个 URL"这条路径关进笼子。

== 这条路径原来是什么样的 ==

三个已接入引擎会在本地抓图（``ehentai.py:90``、``baidu.py:99``，以及未接入的 lenso）：

    files = {"sfile": await self.download(url)}

而 ``HandOver.download``（``network.py:310-333``）是这样实现的::

    async with ClientManager(self.client, self.proxies, self.headers, self.cookies, ...) as client:
        resp = await client.get(url, headers=headers)
        return resp.read()

四个问题叠在一起：

1. **无地址校验** —— 目标可以是 ``http://127.0.0.1:8080/`` 或内网地址。
2. **整包读进内存** —— ``resp.read()`` 先全部读出来再返回；
   ``Content-Length`` 大就 OOM，而且**总超时挡不住它**：一个永远慢慢发、
   总量合法的响应会让连接一直挂着。
3. **继承引擎凭据** —— 用的是同一个 client，cookie 和代理都会跟着去目标站点。
4. **自动跟随重定向** —— 那个 client 是 ``follow_redirects=True``，
   只校验初始 URL 的写法在这条路上没有意义。

== 本模块的取舍 ==

- **只服务本地抓图的那几个引擎。** 其余引擎把 URL 直接交给搜索站点抓
  （Yandex 首次 URL 搜索应继续由 Yandex 抓图）—— 那是"对端去抓"，不是我们的出站面。
- **显式拒绝无法安全处理的组合。** 代理会替我们解析域名，本机的 DNS 校验就无法证明
  代理最终连到哪；所以下载默认**不继承任何代理**，配置了下载代理则拒绝
  （见 ``proxy`` 参数）。
- **DNS 固定连接。** 解析→校验→**用校验过的那个 IP 连接**，同时保留正确的 Host、
  TLS SNI 与证书校验（``extensions={"sni_hostname": ...}``，httpcore 1.0.9 支持）。
  只做"解析一下确认是公网，然后普通 get(url)"是不够的 —— 那会二次解析，可被 DNS 重绑定穿过。
- **每跳重新校验。** 不自动跟随重定向，自己走跳数，逐跳重跑 URL 与地址校验。
"""

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
#: **连接层实际收到的目的地址**（只断言 URL 字符串是测不到 DNS 这一步的）。
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


#: stdlib 的 flag 表**没盖住**的特殊用途网段，必须显式判否。
#: 为什么不能只信 ``is_global``：``fec0::/10``（已废弃的 IPv6 站点本地）在本机
#: Python 3.11.16 上 ``is_global=True``，且 private / reserved 全为 False ——
#: 所有 flag 都不拦，段首、段内、段尾实测全部放行。
#: 本项目只要求 Python >=3.10，而这张表在小版本之间会变；安全判定不该跟着
#: 解释器版本漂移，所以关键的拒绝在这里写死（`is_site_local` 与本表等价，
#: 选显式网段是为了不依赖 flag 语义，也方便继续加同类的段）。
_EXTRA_DENIED_NETWORKS: tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network, ...
] = (
    ipaddress.ip_network("fec0::/10"),       # 已废弃的站点本地（RFC 3879）
    ipaddress.ip_network("192.88.99.0/24"),  # 已废弃的 6to4 中继任播（RFC 7526）
)


def is_public_address(raw: str) -> bool:
    """该地址是否可安全访问。

    拒绝：私网、回环、链路本地、多播、保留、未指定、IPv4 映射/6to4 包装的私网地址，
    以及 :data:`_EXTRA_DENIED_NETWORKS` 里那些 stdlib flag 盖不住的特殊用途网段。
    允许：公网 IPv4 与公网 IPv6（**不能把"覆盖 IPv6"实现成"一律拒绝 IPv6"**）。
    """
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
        """authority 段。**IPv6 字面量必须带方括号** —— 否则 ``host:port`` 会被
        重新解析成别的东西（旧实现把 ``2606:4700::1111:8443`` 当主机名发了出去，
        于是在重定向的下一跳重新 parse 时直接坏掉）。
        """
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
    """解析并规范化 URL。

    ``urlsplit`` 与 ``.port`` **自己就会抛 ValueError**（非法端口、方括号不配对等）。
    这里统一包装成 ``invalid_url``：否则调用方看到的是一条裸 ``ValueError``，
    分不清"用户给了坏 URL"与"服务端出错了"（复核报告 B1 逐个实测了这一串）。
    """
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
    # 直到构造请求时才抛出 UnicodeEncodeError（复核报告 B1 实测「例子.测试」）。
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
    """解析并校验，返回可用的地址列表（顺序即尝试顺序）。

    策略：**只要解析结果里有任何一个非公网地址，整体拒绝。**
    攻击者可以用一条公网记录 + 一条内网记录（多 A 记录）来试运气，
    "挑公网的那个连"看起来更宽容，但一旦将来有人改了选地址逻辑就会破防；
    宁可整体拒绝，行为更好推理。
    """
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
    """带地址校验、跳数控制、字节预算与总期限的下载器。

    明确**不继承**环境代理与引擎凭据：
    ``trust_env=False`` 且默认 ``proxy=None``。原因是这两样都会让上面的地址校验失去意义 ——
    代理替你解析域名，凭据则把引擎身份带给了目标站点。
    """

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
        """取回图片字节。

        **整条调用受一个绝对期限约束**：DNS 解析、每一跳的连接与响应头、读取循环、
        以及失败后的清理，都必须在 ``policy.total_timeout`` 内结束。

        之前这里只把 deadline 的**数值**往下传、在"每跳开始"和"收到 body chunk"处比较
        —— 那盖不住裸 await：resolver 卡住时协程会一直挂着，下载器自己不报 timeout
        （实测：30 ms 期限配一个阻塞的 resolver，只有外层 ``wait_for`` 能停住它）。
        所以在外层再包一层 ``wait_for`` —— 这是唯一能把"正在等待的裸 await"
        也纳入期限的办法。

        不用 ``asyncio.timeout``：那是 3.11+ 才有的，而 pyproject 声明支持 3.10。
        """
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
            # 把**当前**这一跳记进 visited。不记的话，一个指回起点的重定向
            # 要等到再发一次请求之后才被发现（复核报告 B1）。
            if target.display not in visited:
                visited.append(target.display)
            addresses = await resolve_and_validate(target, self.policy, self._resolver)

            response_status, location, body = await self._request_once(
                target, addresses, deadline
            )

            if response_status in REDIRECT_STATUSES:
                # 缺 Location、成环要先判：放在跳数之后的话，在最后一跳
                # 这些更具体的原因会被 too_many_redirects 盖住（复核报告 B1）
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
        """对一个已校验的目标发一次请求（不跟随重定向）。

        会依次尝试解析出的各个地址；全部失败才报错。
        """
        last_error: Exception | None = None
        timed_out = False

        for address in addresses:
            try:
                return await self._request_pinned(target, address, deadline)
            except DownloadError:
                raise
            except httpx.TimeoutException as exc:
                # 单次 I/O 超时与"连不上"对排查的含义完全不同：
                # 过去 ReadTimeout 也被归成 connection_failed（复核报告 B1）
                last_error = exc
                timed_out = True
                continue
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc
                continue

        # 只报异常**类型名**，不把 str(exc) 拼进来：它可能带目标 URL 或本地细节
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
        # 连到**已校验的那个 IP**；Host 与 TLS SNI 仍用原主机名，证书按原主机名校验
        pinned = _pinned_url(target, address)
        headers = {
            "Host": target.host_header,
            "User-Agent": self.policy.user_agent,
            "Accept": "*/*",
            # 明确要**未压缩**的字节。图片本身已是压缩格式，再叠一层内容编码
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
                    # 在**读 body 之前**判定。先读完再报的话，一个大 body 的错误页
                    # 会被归成 too_large（复核报告 B1 实测：503 带超限 Content-Length
                    # 报的是 too_large 而不是 http_error），而且白下载一遍。
                    raise DownloadError(
                        "http_error", f"目标返回 HTTP {status}", url=target.display
                    )

                # Content-Length 只用来提前拒绝，**不能代替**实际计数：
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
                    # 光发 ``Accept-Encoding: identity`` 不够 —— 服务端可以不遵守。
                    # 一旦是 gzip/deflate，httpx 会在 yield 出第一个 chunk **之前**就把
                    # 整个 body 解出来（``_models.py`` 是先 decode 再 yield，而
                    # ``_decoders.py`` 的 gzip 解压没有输出长度上限）。
                    # 实测：32 MiB 数据压成 32 KB，预算设 1 MiB —— 最终确实报 too_large，
                    # 但此前 Python 分配峰值已到 77.41 MiB，防护发生得太晚。
                    # 所以在读之前就拒掉，并把原因说清楚，便于排查是站点行为还是配置问题。
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
        """流式读取：大小上限**和**期限都在这里自己查。

        **两层都要留**，这是实测结论（我先前删过内层，被复核用实测驳回了）：
        外层 ``wait_for`` 是唯一能停住"正在等的裸 await"（例如卡在 resolver 线程里）
        的手段，但它投递取消只能落在 await 点上 —— 20 ms 期限配一个忙转的流，
        实测跑到了 135 ms。内层这个检查不依赖取消，每收到一个 chunk 比一次绝对期限，
        负责把"数据还在稳定到达、但总时限已经过了"当场截停。
        反过来只留内层也不行：卡在裸 await 上时根本轮不到它。

        - **计的是解码后的字节数**（``aiter_bytes``）。但这**不等于内存受控** ——
          httpx 先把 raw 解压、再 yield 出来，解压发生在本函数的计数**之前**，
          所以"已经分配过一大块之后才报超限"是可能的。
          真正的防线在 ``_request_pinned``：读之前就拒绝非 identity 的
          ``Content-Encoding``。
        """
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


#: 会在**本地**抓图的引擎 —— 也就是说，用户给的 URL 会由我们的服务器去访问。
#: 其余引擎把 URL 交给搜索站点自己去抓，那不是本机的出站面。
#: 这个集合由 ``tests/test_safe_download.py`` 对着上游源码核验（谁调用 ``self.download``）。
ENGINES_THAT_FETCH_LOCALLY: frozenset[str] = frozenset({"EHentai", "BaiDu"})
