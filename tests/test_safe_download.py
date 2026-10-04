"""安全下载器的行为与边界。

这些测试**不联网**：解析器是注入的，连接层是注入的。
正例也在这里 —— "所有下载都被拒绝"同样能通过一组纯负向测试，
所以每条拒绝旁边都要有能走通的正例。
"""

from __future__ import annotations

import inspect
import re
import time

import asyncio

import httpx
import pytest
from PicImageSearch import engines as upstream_engines

from image_search_mcp.safe_download import (
    ENGINES_THAT_FETCH_LOCALLY,
    DownloadError,
    DownloadPolicy,
    SafeDownloader,
    is_public_address,
    parse_target,
    resolve_and_validate,
    Target,
)

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:4700:10::6814:179a"


def _resolver(mapping: dict[str, list[str]]):
    async def _resolve(host: str, port: int) -> list[str]:
        if host not in mapping:
            raise OSError(f"name resolution failed for {host}")
        return mapping[host]
    return _resolve


class _Recorder:
    """注入的连接层：记录它实际收到的请求，并按脚本返回响应。"""

    def __init__(self, handler=None):
        self.requests: list[httpx.Request] = []
        self._handler = handler or (lambda r: httpx.Response(200, content=b"IMG"))

    def transport(self) -> httpx.AsyncBaseTransport:
        recorder = self

        class _T(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                recorder.requests.append(request)
                return recorder._handler(request)

        return _T()


def _downloader(recorder: _Recorder, hosts: dict[str, list[str]], **policy_kw):
    return SafeDownloader(
        DownloadPolicy(**policy_kw),
        resolver=_resolver(hosts),
        transport_factory=recorder.transport,
    )


# ==========================================================================
# 契约：哪些引擎会在本地抓图
# ==========================================================================

def test_local_fetch_set_matches_upstream_source():
    """``ENGINES_THAT_FETCH_LOCALLY`` 必须与上游源码一致。

    这个集合决定"哪些引擎的用户 URL 会由我们的服务器去访问"。它写错了，
    安全下载器就装在了错误的地方 —— 要么漏装，要么挂在根本不抓图的引擎上。
    所以从上游源码里把 ``await self.download(`` 的调用点刮出来核对，
    而不是靠记忆维护这张表。
    """
    module_of = {
        "SauceNAO": "saucenao", "Google": "google", "TraceMoe": "tracemoe",
        "Ascii2D": "ascii2d", "BaiDu": "baidu", "Bing": "bing", "EHentai": "ehentai",
        "GoogleLens": "google_lens", "Iqdb": "iqdb", "Tineye": "tineye", "Yandex": "yandex",
    }
    actual = set()
    for engine, module in module_of.items():
        source = inspect.getsource(
            __import__(f"PicImageSearch.engines.{module}", fromlist=["_"])
        )
        if re.search(r"await\s+self\.download\s*\(", source):
            actual.add(engine)

    assert actual == set(ENGINES_THAT_FETCH_LOCALLY), (
        f"上游源码里在本地抓图的是 {sorted(actual)}，"
        f"但契约写的是 {sorted(ENGINES_THAT_FETCH_LOCALLY)}"
    )


def test_the_declared_set_is_not_empty():
    """反向测试的一半：集合不能意外变成空集，否则安全下载器等于没接。"""
    assert ENGINES_THAT_FETCH_LOCALLY


# ==========================================================================
# 地址判定
# ==========================================================================

@pytest.mark.parametrize("addr", [
    "127.0.0.1", "::1", "::ffff:127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1",
    "169.254.169.254",          # 云元数据服务
    "0.0.0.0", "fe80::1", "224.0.0.1", "255.255.255.255",
    "fd00::1",                  # IPv6 唯一本地地址
])
def test_non_public_addresses_are_rejected(addr: str):
    assert is_public_address(addr) is False


@pytest.mark.parametrize("addr", [
    # 这一组是 stdlib 的 flag 表**盖不住**的特殊用途网段：本机 Python 对它们的
    # is_global 为 True、private/reserved 全为 False，所有 flag 都不拦。
    # 这几条只能靠显式网段表拒绝，**不要**当成多余用例删掉。
    "fec0::", "fec0::1", "feff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",  # 站点本地 fec0::/10
    "192.88.99.0", "192.88.99.2", "192.88.99.255",                  # 6to4 中继 /24
])
def test_special_purpose_blocks_flagged_global_are_still_rejected(addr: str):
    """段首 / 段中 / 段尾都要拒 —— 只测一个地址会漏掉整段。"""
    assert is_public_address(addr) is False


@pytest.mark.parametrize("addr", [
    "192.88.98.255",  # 紧邻本段之前
    "192.89.0.0",     # 紧邻本段之后
    PUBLIC_V4, PUBLIC_V6,
    "2606:4700::1111",
])
def test_neighbours_of_the_special_blocks_are_unaffected(addr: str):
    """显式网段表不能顺手把邻居也拒掉：拒绝范围要正好等于那一段。"""
    assert is_public_address(addr) is True


@pytest.mark.parametrize("addr", [
    # stdlib 的 flag 已经覆盖这些，但此前**没有回归用例** —— 缺了它们，
    # 一旦有人把 is_global 的判定顺序改动，没有任何东西会变红。
    "100.64.0.1",              # 运营商级 NAT
    "192.0.2.1", "198.51.100.1", "203.0.113.1",  # 文档用段
    "198.18.0.1",              # 基准测试段
    "::ffff:10.0.0.1",         # IPv4-mapped 私网
    "2002:7f00:1::",           # 6to4 里嵌了 127.0.0.1
    "2001::1",                 # Teredo
    "64:ff9b::1",              # NAT64 前缀（当前整体拒绝，见报告 Q4）
    "2001:db8::1",             # IPv6 文档段
    "fc00::", "fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",  # ULA 段首与段尾
])
def test_special_purpose_ranges_covered_by_stdlib_are_rejected(addr: str):
    assert is_public_address(addr) is False


@pytest.mark.parametrize("addr", [
    "8.8.8.8", PUBLIC_V4,
    PUBLIC_V6, "2001:4860:4860::8888",
])
def test_public_addresses_pass_including_ipv6(addr: str):
    """不能把"覆盖 IPv6"实现成"一律拒绝 IPv6"。"""
    assert is_public_address(addr) is True


def test_garbage_is_not_public():
    assert is_public_address("not-an-address") is False


# ==========================================================================
# 正例：合法目标要能走通
# ==========================================================================

async def test_public_target_is_fetched():
    rec = _Recorder()
    body = await _downloader(rec, {"images.example.com": [PUBLIC_V4]}).fetch(
        "https://images.example.com/a.jpg"
    )
    assert body == b"IMG"
    assert len(rec.requests) == 1


async def test_connection_uses_the_validated_address_with_correct_sni():
    """关键的观测点：挨到连接层的是**已校验的 IP**，而 Host / SNI 仍是原域名。

    只在 MockTransport 里断言 URL 字符串是测不到 DNS 这一步的 ——
    这里断言的就是连接层实际收到的目的地址。
    """
    rec = _Recorder()
    await _downloader(rec, {"images.example.com": [PUBLIC_V4]}).fetch(
        "https://images.example.com/deep/path.jpg?x=1"
    )
    request = rec.requests[0]
    assert request.url.host == PUBLIC_V4, "必须连到已校验的 IP"
    assert request.url.path == "/deep/path.jpg"
    assert request.url.query == b"x=1"
    assert request.headers["host"] == "images.example.com"
    assert request.extensions.get("sni_hostname") == "images.example.com"


async def test_ipv6_target_is_reachable():
    rec = _Recorder()
    body = await _downloader(rec, {"v6.example.com": [PUBLIC_V6]}).fetch(
        "https://v6.example.com/a.jpg"
    )
    assert body == b"IMG"
    assert rec.requests[0].url.host == PUBLIC_V6


# ==========================================================================
# 拒绝：且必须发生在建立连接之前
# ==========================================================================

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/a.jpg",
    "http://localhost/a.jpg",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/a.jpg",
    "http://[::1]/a.jpg",
    "http://[::ffff:127.0.0.1]/a.jpg",
])
async def test_internal_targets_rejected_without_any_connection(url: str):
    rec = _Recorder()
    downloader = _downloader(rec, {"localhost": ["127.0.0.1"]})
    with pytest.raises(DownloadError) as exc:
        await downloader.fetch(url)
    assert exc.value.reason == "blocked_address"
    assert rec.requests == [], "被拒的目标不能产生任何连接"


@pytest.mark.parametrize("url,reason", [
    ("ftp://example.com/a.jpg", "invalid_url"),
    ("file:///etc/passwd", "invalid_url"),
    ("http://user:pw@example.com/a.jpg", "invalid_url"),
    ("", "invalid_url"),
    ("http:///no-host.jpg", "invalid_url"),
])
async def test_bad_urls_rejected_without_any_connection(url: str, reason: str):
    rec = _Recorder()
    downloader = _downloader(rec, {"example.com": [PUBLIC_V4]})
    with pytest.raises(DownloadError) as exc:
        await downloader.fetch(url)
    assert exc.value.reason == reason
    assert rec.requests == []


async def test_mixed_public_and_private_resolution_is_rejected():
    """一条公网记录 + 一条内网记录时整体拒绝，而不是"挑公网那个连"。"""
    rec = _Recorder()
    downloader = _downloader(rec, {"evil.example.com": [PUBLIC_V4, "10.0.0.7"]})
    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("http://evil.example.com/a.jpg")
    assert exc.value.reason == "blocked_address"
    assert rec.requests == []


async def test_dns_failure_is_rejected_not_retried_unvalidated():
    """解析失败必须直接拒绝 —— 不能退回"未校验直接连"。"""
    rec = _Recorder()
    downloader = _downloader(rec, {})  # 任何域名都解析失败
    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("http://unknown.example.com/a.jpg")
    assert exc.value.reason == "dns_failure"
    assert rec.requests == []


# ==========================================================================
# 重定向
# ==========================================================================

def _redirecting(location: str, status: int = 302):
    return lambda request: httpx.Response(status, headers={"location": location})


async def test_redirect_to_public_target_is_followed_and_revalidated():
    rec = _Recorder()
    rec._handler = lambda r: (
        _redirecting("https://b.example.com/b.jpg")(r)
        if r.url.host == PUBLIC_V4 else httpx.Response(200, content=b"IMG2")
    )
    hosts = {"a.example.com": [PUBLIC_V4], "b.example.com": ["1.1.1.1"]}
    body = await _downloader(rec, hosts).fetch("https://a.example.com/a.jpg")
    assert body == b"IMG2"
    assert [r.url.host for r in rec.requests] == [PUBLIC_V4, "1.1.1.1"]


async def test_redirect_to_private_address_is_rejected_after_revalidation():
    """第一跳是公网，重定向到内网 —— 必须逐跳重新校验才拦得住。

    这里刻意用同协议的 http→http，把"地址校验"单独拎出来；
    https→http 那一跳由 ``test_https_to_http_downgrade_is_rejected`` 单独覆盖
    （那种重定向会先命中降级检查，两个原因都对，但混在一起就测不清是哪个生效）。
    """
    rec = _Recorder()
    rec._handler = _redirecting("http://127.0.0.1/secret")
    downloader = _downloader(rec, {"a.example.com": [PUBLIC_V4]})
    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("http://a.example.com/a.jpg")
    assert exc.value.reason == "blocked_address"
    assert len(rec.requests) == 1, "只应发出第一跳，第二跳在连接前就被拒"


async def test_redirect_to_private_hostname_is_rejected():
    """重定向到"解析出内网地址的域名"同样要拦 —— 不能只查字面量 IP。"""
    rec = _Recorder()
    rec._handler = _redirecting("http://internal.example.com/secret")
    downloader = _downloader(
        rec, {"a.example.com": [PUBLIC_V4], "internal.example.com": ["192.168.0.9"]}
    )
    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("http://a.example.com/a.jpg")
    assert exc.value.reason == "blocked_address"
    assert len(rec.requests) == 1


async def test_redirect_dns_rebinding_is_rejected():
    """同一域名第二跳解析到内网 —— 每次都要重跑 DNS 校验。"""
    calls = {"n": 0}

    async def _flip(host: str, port: int) -> list[str]:
        calls["n"] += 1
        return [PUBLIC_V4] if calls["n"] == 1 else ["10.0.0.9"]

    rec = _Recorder()
    rec._handler = _redirecting("http://same.example.com/second")
    downloader = SafeDownloader(
        DownloadPolicy(), resolver=_flip, transport_factory=rec.transport
    )
    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("http://same.example.com/first")
    assert exc.value.reason == "blocked_address"


async def test_https_to_http_downgrade_is_rejected():
    rec = _Recorder()
    rec._handler = _redirecting("http://a.example.com/insecure.jpg")
    downloader = _downloader(rec, {"a.example.com": [PUBLIC_V4]})
    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("https://a.example.com/a.jpg")
    assert exc.value.reason == "https_downgrade"


async def test_too_many_redirects():
    rec = _Recorder()
    counter = {"n": 0}

    def _handler(request):
        counter["n"] += 1
        return httpx.Response(302, headers={"location": f"/hop{counter['n']}"})

    rec._handler = _handler
    downloader = _downloader(rec, {"a.example.com": [PUBLIC_V4]}, max_redirects=2)
    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("https://a.example.com/a.jpg")
    assert exc.value.reason == "too_many_redirects"
    assert len(rec.requests) == 3  # 首次 + 2 跳


async def test_redirect_loop_is_detected():
    rec = _Recorder()
    rec._handler = _redirecting("/loop")
    downloader = _downloader(rec, {"a.example.com": [PUBLIC_V4]}, max_redirects=5)
    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("https://a.example.com/loop")
    assert exc.value.reason == "redirect_loop"


# ==========================================================================
# 预算
# ==========================================================================

async def test_content_length_over_budget_is_rejected_early():
    rec = _Recorder(lambda r: httpx.Response(
        200, headers={"content-length": "99999999"}, content=b"x" * 10
    ))
    downloader = _downloader(rec, {"a.example.com": [PUBLIC_V4]}, max_bytes=1024)
    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("http://a.example.com/a.jpg")
    assert exc.value.reason == "too_large"


async def test_chunked_response_is_capped_without_content_length():
    """分块响应没有 Content-Length —— 只能靠流式累计兜住。"""
    def _handler(request):
        async def _gen():
            for _ in range(50):
                yield b"y" * 100
        return httpx.Response(200, content=_gen())

    rec = _Recorder(_handler)
    downloader = _downloader(rec, {"a.example.com": [PUBLIC_V4]}, max_bytes=1000)
    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("http://a.example.com/a.jpg")
    assert exc.value.reason == "too_large"


async def test_lying_content_length_still_capped():
    """Content-Length 报小了也不能突破实际计数。"""
    def _handler(request):
        async def _gen():
            for _ in range(50):
                yield b"z" * 100
        return httpx.Response(
            200, headers={"content-length": "10"}, content=_gen()
        )

    rec = _Recorder(_handler)
    downloader = _downloader(rec, {"a.example.com": [PUBLIC_V4]}, max_bytes=1000)
    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("http://a.example.com/a.jpg")
    assert exc.value.reason == "too_large"


async def test_body_within_budget_is_returned_whole():
    """正例：预算内的正常响应必须完整返回。"""
    rec = _Recorder(lambda r: httpx.Response(200, content=b"a" * 500))
    downloader = _downloader(rec, {"a.example.com": [PUBLIC_V4]}, max_bytes=1000)
    assert len(await downloader.fetch("http://a.example.com/a.jpg")) == 500


async def test_empty_body_is_rejected():
    rec = _Recorder(lambda r: httpx.Response(200, content=b""))
    downloader = _downloader(rec, {"a.example.com": [PUBLIC_V4]})
    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("http://a.example.com/a.jpg")
    assert exc.value.reason == "empty_body"


async def test_http_error_status_is_reported():
    rec = _Recorder(lambda r: httpx.Response(503))
    downloader = _downloader(rec, {"a.example.com": [PUBLIC_V4]})
    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("http://a.example.com/a.jpg")
    assert exc.value.reason == "http_error"
    assert "503" in str(exc.value)


async def test_slow_trickle_is_stopped_by_total_deadline():
    """大小合法、总也发不完的响应 —— 只有总期限能终止它。

    两个刻意的设计：

    - 响应体是**有限**的（80 块 × 30ms ≈ 2.4s）。总期限检查失效时，这个下载会
      正常结束、不抛异常，测试以一条干净的断言失败收场。写成 ``while True``
      也能"发现问题"，但那是以挂住 30 秒、抛一屏超时堆栈的方式发现的 ——
      CI 上表现为"卡住"，比失败难查得多。
    - 只接受 :class:`DownloadError`，且断言 ``reason == "timeout"``。
      旧版本收的是 ``(DownloadError, httpx.HTTPError, TimeoutError)`` 这样一个
      宽泛元组 —— 那等于"只要因为任何原因失败就算通过"，把这条测试变成了
      "下载器没成功"的同义反复。
    """
    chunks, delay = 80, 0.03

    def _handler(request):
        async def _gen():
            for _ in range(chunks):
                yield b"a"
                await asyncio.sleep(delay)
        return httpx.Response(200, content=_gen())

    rec = _Recorder(_handler)
    downloader = _downloader(
        rec, {"a.example.com": [PUBLIC_V4]}, max_bytes=10 ** 9, total_timeout=0.4
    )

    started = time.monotonic()
    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("http://a.example.com/a.jpg")
    elapsed = time.monotonic() - started

    assert exc.value.reason == "timeout", f"应以 timeout 结束，实际 {exc.value.reason}"
    assert elapsed < 1.5, (
        f"应当在总期限附近就放弃，而不是等整个响应发完（{chunks * delay:.1f}s）："
        f"实际 {elapsed:.2f}s"
    )


async def test_trickle_that_fits_in_the_budget_still_succeeds():
    """正对照：同样慢的响应，只要在总期限之内就必须成功。

    没有这条，"一律拒绝慢响应"也能通过上面那一条。
    """
    def _handler(request):
        async def _gen():
            for _ in range(5):
                yield b"abc"
                await asyncio.sleep(0.01)
        return httpx.Response(200, content=_gen())

    rec = _Recorder(_handler)
    downloader = _downloader(
        rec, {"a.example.com": [PUBLIC_V4]}, max_bytes=10 ** 6, total_timeout=5.0
    )
    assert await downloader.fetch("http://a.example.com/a.jpg") == b"abc" * 5


# ==========================================================================
# 凭据与代理隔离
# ==========================================================================

async def test_downloader_does_not_inherit_environment_proxy(monkeypatch):
    """``trust_env`` 关掉：环境里的代理不能影响下载。

    这是实测踩过的：``HTTP_PROXY`` 存在时，旧代码会让带凭据的请求悄悄走那个代理。
    """
    monkeypatch.setenv("HTTP_PROXY", "http://192.0.2.1:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://192.0.2.1:3128")
    rec = _Recorder()
    await _downloader(rec, {"a.example.com": [PUBLIC_V4]}).fetch("http://a.example.com/a.jpg")
    assert len(rec.requests) == 1, "环境代理不应改变出站路径"


async def test_explicit_download_proxy_is_rejected():
    """代理会代替本机解析域名 ⇒ DNS 校验失效 ⇒ 明确拒绝而不是默默放行。"""
    with pytest.raises(DownloadError) as exc:
        SafeDownloader(proxy="http://127.0.0.1:3128")
    assert exc.value.reason == "unsupported_proxy"


async def test_downloader_sends_no_cookies():
    """用户图源下载不带任何引擎凭据。"""
    rec = _Recorder()
    await _downloader(rec, {"a.example.com": [PUBLIC_V4]}).fetch("http://a.example.com/a.jpg")
    assert "cookie" not in rec.requests[0].headers


# ==========================================================================
# 连接层重试
# ==========================================================================

async def test_second_address_is_tried_when_the_first_fails():
    attempts = []

    def _handler(request):
        attempts.append(request.url.host)
        if request.url.host == PUBLIC_V4:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, content=b"OK2")

    rec = _Recorder(_handler)
    downloader = _downloader(rec, {"a.example.com": [PUBLIC_V4, "1.1.1.1"]})
    assert await downloader.fetch("http://a.example.com/a.jpg") == b"OK2"
    assert attempts == [PUBLIC_V4, "1.1.1.1"]


async def test_all_addresses_failing_reports_connection_failed():
    def _handler(request):
        raise httpx.ConnectError("nope")

    rec = _Recorder(_handler)
    downloader = _downloader(rec, {"a.example.com": [PUBLIC_V4, "1.1.1.1"]})
    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("http://a.example.com/a.jpg")
    assert exc.value.reason == "connection_failed"


# ==========================================================================
# 策略参数
# ==========================================================================

def test_policy_rejects_nonsense_values():
    with pytest.raises(ValueError):
        DownloadPolicy(max_bytes=0)
    with pytest.raises(ValueError):
        DownloadPolicy(max_redirects=-1)
    with pytest.raises(ValueError):
        DownloadPolicy(total_timeout=0)


def test_parse_target_keeps_non_default_port():
    target = parse_target("https://a.example.com:8443/x", DownloadPolicy())
    assert target.port == 8443
    assert target.host_header == "a.example.com:8443"


async def test_resolve_and_validate_accepts_literal_addresses():
    """字面量 IP 也要过校验，不能因为"不用解析"就跳过。"""
    downloader = SafeDownloader(DownloadPolicy(), resolver=_resolver({}))
    from image_search_mcp.safe_download import parse_target
    with pytest.raises(DownloadError) as exc:
        await resolve_and_validate(
            parse_target("http://10.0.0.1/x", downloader.policy),
            downloader.policy,
            downloader._resolver,
        )
    assert exc.value.reason == "blocked_address"


# ==========================================================================
# 总期限必须包住"裸 await"（复核报告 A1）
# ==========================================================================

async def test_resolver_that_never_returns_hits_the_deadline():
    """resolver 卡住时，下载器必须**自己**报 timeout —— 不能一直挂着。

    期限原来只在"每跳开始"和"收到 body chunk"处比较，那两个检查点都在裸 await
    **之外**：resolver 只要不返回，两个点都到不了，协程就永远停在那儿。
    实测：探针只能靠外层 ``wait_for`` 停住，下载器自己从不报错。

    去掉 ``fetch`` 里的 ``wait_for``，这条会一直挂到 pytest 的 30s 超时 —— 变红。
    """

    async def _never(host: str, port: int) -> list[str]:
        await asyncio.Event().wait()  # 永不返回
        return []

    downloader = SafeDownloader(
        DownloadPolicy(total_timeout=0.05),
        resolver=_never,
        transport_factory=_Recorder().transport,
    )
    started = time.monotonic()
    with pytest.raises(DownloadError) as exc:
        # 测试自己再加一层 5s 上限。理由：把外层期限注入掉之后（反向测试会这么做），
        # 这条应当变成**干净的断言失败**，而不是挂到 pytest-timeout 才结束 ——
        # "卡住"在 CI 上比"失败"贵得多，而且很容易被人靠调大超时糊过去。
        await asyncio.wait_for(downloader.fetch("https://a.example.com/a.jpg"), 5)
    assert exc.value.reason == "timeout"
    # 不能靠上面那层 5s 兜底：必须是下载器自己按时限收手
    assert time.monotonic() - started < 3


# ==========================================================================
# 内容编码：解压发生在计数之前（复核报告 A2）
# ==========================================================================

def _gzip_of_repeated_bytes(total: int) -> bytes:
    import gzip

    return gzip.compress(b"\0" * total)


async def test_compressed_response_is_refused_before_it_is_decompressed():
    """带 ``Content-Encoding`` 的响应必须在**读取之前**拒绝。

    为什么不能只靠"读的时候数字节"：httpx 是先 decode 再 yield
    （``_models.py`` 先 ``decoder.decode(raw)``，``_decoders.py`` 的 gzip
    解压没有输出长度上限）。实测：32 MiB 压成 32 KB、预算 1 MiB，
    最终确实报 ``too_large``，但**此前 Python 分配峰值已到 77.41 MiB**。

    所以断言的是**拒绝的理由**，而不是"最终拒绝了"——
    只有前者能证明解压根本没发生。
    """
    payload = _gzip_of_repeated_bytes(32 * 1024 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("accept-encoding") == "identity", (
            "必须显式要求未压缩的字节：缺了这个头就是在邀请对方压缩响应"
        )
        return httpx.Response(200, content=payload, headers={"Content-Encoding": "gzip"})

    downloader = _downloader(
        _Recorder(handler), {"a.example.com": [PUBLIC_V4]}, max_bytes=1024 * 1024
    )
    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("https://a.example.com/a.jpg")
    assert exc.value.reason == "unsupported_content_encoding"


async def test_large_uncompressed_response_still_reports_too_large():
    """正对照：不带内容编码的超限响应，仍然按 ``too_large`` 拒绝。

    没有这条，"拒绝得早"可能被实现成"什么都拒绝"。
    """
    payload = b"\0" * (4 * 1024 * 1024)
    downloader = _downloader(
        _Recorder(lambda r: httpx.Response(200, content=payload)),
        {"a.example.com": [PUBLIC_V4]},
        max_bytes=1024 * 1024,
    )
    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("https://a.example.com/a.jpg")
    assert exc.value.reason == "too_large"


@pytest.mark.parametrize("encoding", ["identity", ""])
async def test_identity_or_absent_encoding_is_accepted(encoding: str):
    """``identity``（或压根没有这个头）必须放行 —— 否则等于拒绝所有正常图源。"""
    headers = {"Content-Encoding": encoding} if encoding else {}
    downloader = _downloader(
        _Recorder(lambda r: httpx.Response(200, content=b"IMG", headers=headers)),
        {"a.example.com": [PUBLIC_V4]},
    )
    assert await downloader.fetch("https://a.example.com/a.jpg") == b"IMG"
