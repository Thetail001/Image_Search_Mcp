"""URL 规范化与失败原因归类（复核报告 B1）。

这里盯的是"错误回给调用方的形状"：

- 畸形 URL 必须是 ``invalid_url``，不能漏出裸 ``ValueError``；
- IPv6 字面量在 authority 段里必须带方括号，否则下一跳重新解析时坏掉；
- 非 ASCII 主机名必须先转 IDNA，不能等到构造请求时才抛 ``UnicodeEncodeError``；
- 原因要分得开：单次 I/O 超时 ≠ 连不上，HTTP 状态 ≠ 体积超限。

每条拒绝旁边都有能走通的正例 —— "什么都拒绝"同样能通过一组纯负向测试。
"""

from __future__ import annotations

import httpx
import pytest

from image_search_mcp.safe_download import (
    DownloadError,
    DownloadPolicy,
    SafeDownloader,
    parse_target,
)

from test_safe_download import PUBLIC_V4, _Recorder, _resolver


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("https://a.example.com:abc/x", "端口不是数字"),
        ("https://a.example.com:99999/x", "端口越界"),
        ("https://a.example.com:0/x", "端口为 0"),
        ("http://[2606:4700::1111/x", "方括号不配对"),
    ],
)
def test_malformed_urls_are_invalid_url_not_bare_valueerror(url: str, why: str) -> None:
    """``urlsplit`` / ``.port`` 自己会抛 ``ValueError``，必须被包装成 invalid_url。

    漏出裸异常的话，调用方分不清"用户给了坏 URL"和"服务端出错了"。
    """
    with pytest.raises(DownloadError) as exc:
        parse_target(url, DownloadPolicy())

    assert exc.value.reason == "invalid_url", why


def test_ipv6_literal_keeps_brackets_in_authority() -> None:
    """不带方括号的话 ``host:port`` 会被重新解析成别的主机名。"""
    target = parse_target("https://[2606:4700::1111]:8443/x", DownloadPolicy())

    assert target.host_header == "[2606:4700::1111]:8443"
    assert target.display == "https://[2606:4700::1111]:8443/x"


def test_ipv6_literal_omits_default_port() -> None:
    target = parse_target("https://[2606:4700::1111]/x", DownloadPolicy())

    assert target.display == "https://[2606:4700::1111]/x"


def test_non_ascii_host_is_idna_encoded() -> None:
    """非 ASCII 主机名要在这里就转成 ASCII，而不是留给构造请求时炸。"""
    target = parse_target("https://例子.测试/x", DownloadPolicy())

    assert target.host == "xn--fsqu00a.xn--0zwm56d"
    assert target.host.isascii()


def test_ipv4_literal_is_not_mangled_by_idna() -> None:
    target = parse_target("https://93.184.216.34:8443/x", DownloadPolicy())

    assert target.host == "93.184.216.34"
    assert target.display == "https://93.184.216.34:8443/x"


@pytest.mark.asyncio
async def test_read_timeout_is_classified_as_timeout_not_connection_failed() -> None:
    """单次 I/O 超时和"连不上"对排查的含义完全不同。"""

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    downloader = SafeDownloader(
        DownloadPolicy(),
        resolver=_resolver({"a.example.com": [PUBLIC_V4]}),
        transport_factory=_Recorder(_boom).transport,
    )

    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("https://a.example.com/x")

    assert exc.value.reason == "timeout"


@pytest.mark.asyncio
async def test_http_error_is_decided_before_the_size_budget() -> None:
    """大 body 的错误页不该被归成 too_large，也不该真去下一个大文件回来。"""

    def _big_503(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(503, content=b"x")
        response.headers["content-length"] = str(DownloadPolicy().max_bytes + 1)
        return response

    downloader = SafeDownloader(
        DownloadPolicy(),
        resolver=_resolver({"a.example.com": [PUBLIC_V4]}),
        transport_factory=_Recorder(_big_503).transport,
    )

    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("https://a.example.com/x")

    assert exc.value.reason == "http_error"


@pytest.mark.asyncio
async def test_oversized_body_still_reports_too_large() -> None:
    """正对照：状态正常时，体积判定必须还是它说了算。"""

    def _big_200(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, content=b"x")
        response.headers["content-length"] = str(DownloadPolicy().max_bytes + 1)
        return response

    downloader = SafeDownloader(
        DownloadPolicy(),
        resolver=_resolver({"a.example.com": [PUBLIC_V4]}),
        transport_factory=_Recorder(_big_200).transport,
    )

    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("https://a.example.com/x")

    assert exc.value.reason == "too_large"


@pytest.mark.asyncio
async def test_redirect_to_itself_is_detected_without_a_second_request() -> None:
    """起点也要算进 visited，否则自环要多发一次请求才被发现。"""

    def _self_loop(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/x"})

    recorder = _Recorder(_self_loop)
    downloader = SafeDownloader(
        DownloadPolicy(),
        resolver=_resolver({"a.example.com": [PUBLIC_V4]}),
        transport_factory=recorder.transport,
    )

    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("https://a.example.com/x")

    assert exc.value.reason == "redirect_loop"
    assert len(recorder.requests) == 1


@pytest.mark.asyncio
async def test_ping_pong_redirect_is_detected() -> None:
    """正例旁边的负例：绕一圈回来同样是成环。"""

    def _ping_pong(request: httpx.Request) -> httpx.Response:
        nxt = "/b" if request.url.path == "/a" else "/a"
        return httpx.Response(302, headers={"location": nxt})

    recorder = _Recorder(_ping_pong)
    downloader = SafeDownloader(
        DownloadPolicy(),
        resolver=_resolver({"a.example.com": [PUBLIC_V4]}),
        transport_factory=recorder.transport,
    )

    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("https://a.example.com/a")

    assert exc.value.reason == "redirect_loop"
    assert len(recorder.requests) == 2


@pytest.mark.asyncio
async def test_missing_location_on_last_hop_reports_the_specific_reason() -> None:
    """具体原因不能被 too_many_redirects 盖住。"""

    def _no_location(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    downloader = SafeDownloader(
        DownloadPolicy(max_redirects=0),
        resolver=_resolver({"a.example.com": [PUBLIC_V4]}),
        transport_factory=_Recorder(_no_location).transport,
    )

    with pytest.raises(DownloadError) as exc:
        await downloader.fetch("https://a.example.com/x")

    assert exc.value.reason == "redirect_without_location"


@pytest.mark.asyncio
async def test_normal_fetch_and_single_hop_redirect_still_work() -> None:
    """两条正例：别把主路和正常跳转改坏。"""
    plain = _Recorder()
    downloader = SafeDownloader(
        DownloadPolicy(),
        resolver=_resolver({"a.example.com": [PUBLIC_V4]}),
        transport_factory=plain.transport,
    )
    assert await downloader.fetch("https://a.example.com/x") == b"IMG"
    assert len(plain.requests) == 1

    def _one_hop(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x":
            return httpx.Response(302, headers={"location": "/y"})
        return httpx.Response(200, content=b"IMG2")

    hopped = _Recorder(_one_hop)
    downloader = SafeDownloader(
        DownloadPolicy(),
        resolver=_resolver({"a.example.com": [PUBLIC_V4]}),
        transport_factory=hopped.transport,
    )
    assert await downloader.fetch("https://a.example.com/x") == b"IMG2"
    assert len(hopped.requests) == 2
