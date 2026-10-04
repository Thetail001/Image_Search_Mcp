"""反向测试用的"故障注入"定义。

每一条把**已经被修掉的缺陷重新放回代码里**，然后指定一个应当因此变红的测试。
判定标准只有两条，缺一不可：

1. 被注入的模块确实是临时副本里的那一份（否则跑的还是干净代码，反向测试是假的）
2. 指定的那条测试确实红了

只跑"能过"的测试不叫门禁 —— 一条永远绿的门禁和没有门禁一样，甚至更糟：
它让人以为有人在看着。所以这里的每一条都必须先证明门禁会红。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BugInjection:
    """一次故障注入。"""

    name: str
    #: 相对仓库根的文件路径
    path: str
    #: 要被替换掉的源码片段（必须与文件中**恰好一处**匹配）
    old: str
    #: 替换成什么
    new: str
    #: 哪条测试负责抓它
    test: str
    #: 它对应哪个原始缺陷（写清"为什么这条门禁值得存在"）
    why: str


INJECTIONS: tuple[BugInjection, ...] = (
    # ---------------------------------------------------------------- 批次 1-1
    BugInjection(
        name="extras-whitelist-removed",
        path="src/image_search_mcp/server.py",
        old="    init_kwargs, search_kwargs = params.validate_extra_params(engine, raw_extra)",
        new="    init_kwargs, search_kwargs = {}, dict(raw_extra)",
        test="tests/test_extras_whitelist.py::test_reserved_file_key_never_reaches_the_filesystem",
        why="任意文件读取：调用方用 extra_params 覆盖 file/url 等输入",
    ),
    BugInjection(
        name="extras-applied-after-input",
        path="src/image_search_mcp/server.py",
        old="    search_kwargs.update(search_args)",
        new="    search_kwargs.update(search_args)\n    search_kwargs.update(raw_extra)",
        test=(
            "tests/test_extras_whitelist.py"
            "::test_input_is_applied_after_extras_so_it_cannot_be_overridden"
        ),
        why="输入被调用方参数覆盖：旧代码是「先装输入、再 update(extras)」",
    ),
    # ---------------------------------------------------------------- 批次 1-3
    BugInjection(
        name="error-leaks-exception-text",
        path="src/image_search_mcp/server.py",
        old=(
            "        return SearchOutcome(\n"
            "            text=(\n"
            '                f"Error: 搜索过程中发生内部错误（{type(exc).__name__}）。"\n'
            '                "详细信息见服务端日志。"\n'
            "            ),\n"
            "            is_error=True,\n"
            "        )"
        ),
        new=(
            "        return SearchOutcome(\n"
            '            text=f"Error: 内部错误（{type(exc).__name__}）: {exc}",\n'
            "            is_error=True,\n"
            "        )"
        ),
        test=(
            "tests/test_search_flow.py"
            "::test_unexpected_exception_is_reported_without_details"
        ),
        why="错误信息带出服务端路径等细节（旧代码拼 traceback.format_exc()）",
    ),
    BugInjection(
        name="limit-bounds-removed",
        path="src/image_search_mcp/server.py",
        old=(
            "    if not (LIMIT_MIN <= limit <= LIMIT_MAX):\n"
            '        raise SearchInputError(f"limit 必须在 {LIMIT_MIN}-{LIMIT_MAX} 之间（收到 {limit}）")'
        ),
        new="    pass  # 注入：范围校验被去掉",
        test=(
            "tests/test_search_flow.py"
            "::test_out_of_range_limit_is_rejected_without_any_request"
        ),
        why="limit 越界：旧代码直接拿去切片，报「top -1」却返回 2 条，limit=0 还会真的发请求",
    ),
    # ---------------------------------------------------------- 批次 1-4 / 1-6
    BugInjection(
        name="cookie-domain-scope-removed",
        path="src/image_search_mcp/credentials.py",
        old=(
            "                    domain=domain,\n"
            "                    domain_specified=True,"
        ),
        new=(
            '                    domain="",  # 注入：去掉域限定\n'
            "                    domain_specified=False,"
        ),
        test=(
            "tests/test_credentials.py"
            "::test_scoped_jar_only_sends_to_the_engine_domain"
        ),
        why="凭据外发：无域 cookie 会跟着请求发往任何域名（实测过 sid 发给 untrusted.invalid）",
    ),
    # ---------------------------------------------------------------- 批次 1-5
    BugInjection(
        name="start-without-token",
        path="src/image_search_mcp/main.py",
        old="    if not token:\n        raise SystemExit(",
        new='    if not token:\n        return ""\n    if False:\n        raise SystemExit(',
        test="tests/test_auth_asgi.py::test_missing_token_refuses_to_start",
        why="缺 token 仍然启动 —— 旧行为是起一个任何人都能调的服务",
    ),
    BugInjection(
        name="root-path-anonymous-exempt",
        path="src/image_search_mcp/main.py",
        old=(
            "        # 豁免收窄成一个只读端点，且只对安全方法生效\n"
            "        if path == HEALTH_PATH and method in SAFE_METHODS:"
        ),
        new=(
            '        # 注入：恢复旧的"根路径无条件放行"\n'
            '        if path == "/":\n'
            "            await self.app(scope, receive, send)\n"
            "            return\n"
            "        if path == HEALTH_PATH and method in SAFE_METHODS:"
        ),
        test="tests/test_auth_asgi.py::test_root_path_is_no_longer_anonymous",
        why="认证绕过：根路径按路径无条件放行，任意方法都进得去",
    ),
    BugInjection(
        name="sse-mounted-at-root",
        path="src/image_search_mcp/main.py",
        old='        return mcp.http_app(transport="sse", path="/sse")',
        new='        return mcp.http_app(transport="sse", path="/")',
        test="tests/test_auth_asgi.py::test_protocol_endpoint_is_not_mounted_at_root",
        why="静默降级：协议端点挂到根路径，和根路径豁免叠加就是匿名可用",
    ),
    # ---------------------------------------------------------------- 文档
    BugInjection(
        name="readme-bad-param-name",
        path="README.md",
        old=r'\"cut_borders\": false',
        new=r'\"cutBorders\": false',
        test=(
            "tests/test_readme_examples.py"
            "::test_readme_examples_have_valid_extra_params"
        ),
        why="文档腐烂：参数名写错（驼峰 vs 下划线）时没有任何东西会红，"
            "读者照着抄只会得到『设置了但没反应』",
    ),
    # ------------------------------------------- 复核报告 A：应当立刻修的四条
    BugInjection(
        name="download-deadline-not-wrapping-bare-awaits",
        path="src/image_search_mcp/safe_download.py",
        old=(
            "        budget = self.policy.total_timeout\n"
            "        try:\n"
            "            return await asyncio.wait_for(self._fetch_hops(url, budget), budget)\n"
            "        except asyncio.TimeoutError as exc:\n"
            '            raise DownloadError("timeout", "下载总期限已到", url=url) from exc'
        ),
        new=(
            "        budget = self.policy.total_timeout\n"
            "        # 注入：去掉外层绝对期限，期限只在检查点比较\n"
            "        return await self._fetch_hops(url, budget)"
        ),
        test=(
            "tests/test_safe_download.py"
            "::test_resolver_that_never_returns_hits_the_deadline"
        ),
        why=(
            "期限盖不住裸 await：只在「每跳开始」和「收到 chunk」处比较，"
            "resolver 卡住时两个检查点都到不了，协程永远挂着、自己不报 timeout。"
            "**只有这条测试能区分外层机制** —— 另有一条注入（原名 "
            "download-total-deadline-removed）改的是同一处代码、却指给「慢速滴流」那条测试；"
            "内层期限检查恢复后，滴流会被内层截停，那条注入就再也区分不出外层了"
            "（实测：注入之后滴流测试仍然通过）。两条注入改的既然是同一处，"
            "就合并成这一条，外层由卡住的 resolver 盯，滴流/内层由 "
            "read-bounded-deadline-check-removed 盯 —— 两个机制各有一条能区分它的测试。"
        ),
    ),
    BugInjection(
        name="content-encoding-rejection-removed",
        path="src/image_search_mcp/safe_download.py",
        old=(
            '                encoding = (response.headers.get("content-encoding") or "").strip().lower()\n'
            '                if encoding and encoding != "identity":'
        ),
        new=(
            '                encoding = ""  # 注入：不再在读之前拒绝压缩响应\n'
            "                if False:"
        ),
        test=(
            "tests/test_safe_download.py"
            "::test_compressed_response_is_refused_before_it_is_decompressed"
        ),
        why="解压发生在计数之前：httpx 先 decode 再 yield，gzip 解压没有输出上限 ——"
            "实测 32 MiB 压成 32 KB、预算 1 MiB，最终报 too_large，"
            "但此前分配峰值已到 77.41 MiB",
    ),
    BugInjection(
        name="extra-denied-networks-removed",
        path="src/image_search_mcp/safe_download.py",
        old=(
            "    # 跨版本稳定的显式拒绝，放在 flag 之前判\n"
            "    if any(addr in net for net in _EXTRA_DENIED_NETWORKS):\n"
            "        return False"
        ),
        new="    pass  # 注入：显式网段表被去掉，只靠 stdlib 的 flag",
        test=(
            "tests/test_safe_download.py"
            "::test_special_purpose_blocks_flagged_global_are_still_rejected"
        ),
        why="fec0::/10 在本机 Python 上 is_global=True 且 private/reserved 全 False，"
            "所有 flag 都不拦 —— 只能靠显式网段表",
    ),
    BugInjection(
        name="cookie-value-equals-rejected",
        path="src/image_search_mcp/credentials.py",
        old='        name, sep, value = segment.partition("=")',
        new=(
            '        if segment.count("=") > 1:\n'
            "            raise CredentialError(\n"
            '                f"{engine} 的 cookies 第 {index} 个片段的值里不能有 =" ,\n'
            "                hint=\"格式为 'name=value'\",\n"
            "            )\n"
            '        name, sep, value = segment.partition("=")'
        ),
        test="tests/test_credentials.py::test_cookie_values_may_contain_equals",
        why="过严：旧正则禁止值里出现 '='，把 base64 编码的凭据（sid=YWJjZA==）全部误拒",
    ),
    BugInjection(
        name="cookie-error-echoes-value",
        path="src/image_search_mcp/credentials.py",
        old='                f"{engine} 的 cookies 第 {index} 个片段的名称不合法",',
        new='                f"{engine} 的 cookies 片段 {segment!r} 的名称不合法",',
        test="tests/test_credentials.py::test_cookie_error_does_not_echo_credential_value",
        why="错误信息回显凭据值，而它会经 server 的错误出口返回给调用方 ——"
            "等于把 cookie 值写进对方的日志",
    ),
    BugInjection(
        name="cookie-control-chars-accepted",
        path="src/image_search_mcp/credentials.py",
        old="        if _COOKIE_VALUE_FORBIDDEN.search(value):",
        new="        if False:  # 注入：值里的控制字符不再检查",
        test="tests/test_credentials.py::test_malformed_cookie_segments_are_rejected",
        why="过宽：旧校验放行名称里的空格与值里的 NUL，会污染实际发出去的请求头",
    ),
    # ------------------------------------------- 复核报告 B1：URL 规范化与原因归类
    BugInjection(
        name="ipv6-authority-brackets-dropped",
        path="src/image_search_mcp/safe_download.py",
        old='        host = f"[{self.host}]" if ":" in self.host else self.host',
        new="        host = self.host",
        test=(
            "tests/test_url_and_reason_contract.py"
            "::test_ipv6_literal_keeps_brackets_in_authority"
        ),
        why="去掉方括号后 Host 头成了『2606:4700::1111:8443』，下一跳重新解析时直接坏掉",
    ),
    BugInjection(
        name="url-parse-valueerror-unwrapped",
        path="src/image_search_mcp/safe_download.py",
        old=(
            "    try:\n"
            "        parts = urlsplit(url.strip())\n"
            '        scheme = (parts.scheme or "").lower()\n'
            "        hostname = parts.hostname\n"
            "        port = parts.port\n"
            "    except ValueError as exc:\n"
            '        raise DownloadError("invalid_url", f"URL 无法解析：{exc}", url=url) from exc'
        ),
        new=(
            "    parts = urlsplit(url.strip())\n"
            '    scheme = (parts.scheme or "").lower()\n'
            "    hostname = parts.hostname\n"
            "    port = parts.port"
        ),
        test=(
            "tests/test_url_and_reason_contract.py"
            "::test_malformed_urls_are_invalid_url_not_bare_valueerror"
        ),
        why="非法端口与不配对方括号会漏出裸 ValueError，调用方分不清是谁的错",
    ),
    BugInjection(
        name="idna-encoding-removed",
        path="src/image_search_mcp/safe_download.py",
        old=(
            '    bare_host = hostname.rstrip(".")\n'
            "    try:\n"
            '        ipaddress.ip_address(bare_host.split("%")[0])  # 字面量地址：原样保留\n'
            "        host = bare_host\n"
            "    except ValueError:\n"
            "        try:\n"
            '            host = bare_host.encode("idna").decode("ascii")\n'
            "        except (UnicodeError, ValueError) as exc:\n"
            "            raise DownloadError(\n"
            '                "invalid_url", f"主机名无法转为 IDNA：{exc}", url=url\n'
            "            ) from exc"
        ),
        new='    host = hostname.rstrip(".")',
        test=(
            "tests/test_url_and_reason_contract.py::test_non_ascii_host_is_idna_encoded"
        ),
        why="中文主机名会被原样放进 Host 头，直到构造请求时才抛 UnicodeEncodeError",
    ),
    BugInjection(
        name="http-status-check-after-size-budget",
        path="src/image_search_mcp/safe_download.py",
        old=(
            "                if status >= 400:\n"
            "                    # 在**读 body 之前**判定。先读完再报的话，一个大 body 的错误页\n"
            "                    # 会被归成 too_large（复核报告 B1 实测：503 带超限 Content-Length\n"
            "                    # 报的是 too_large 而不是 http_error），而且白下载一遍。\n"
            "                    raise DownloadError(\n"
            '                        "http_error", f"目标返回 HTTP {status}", url=target.display\n'
            "                    )\n"
            "\n"
        ),
        new="",
        test=(
            "tests/test_url_and_reason_contract.py"
            "::test_http_error_is_decided_before_the_size_budget"
        ),
        why="先按体积判、再按状态判：大 body 的错误页会被报成 too_large，还白下载一遍",
    ),
    BugInjection(
        name="read-timeout-lumped-into-connection-failed",
        path="src/image_search_mcp/safe_download.py",
        old=(
            "            except httpx.TimeoutException as exc:\n"
            '                # 单次 I/O 超时与"连不上"对排查的含义完全不同：\n'
            "                # 过去 ReadTimeout 也被归成 connection_failed（复核报告 B1）\n"
            "                last_error = exc\n"
            "                timed_out = True\n"
            "                continue\n"
        ),
        new="",
        test=(
            "tests/test_url_and_reason_contract.py"
            "::test_read_timeout_is_classified_as_timeout_not_connection_failed"
        ),
        why="ReadTimeout 落进连接失败分支后，排查时会把『对端慢』看成『连不上』",
    ),
    BugInjection(
        name="initial-url-not-marked-visited",
        path="src/image_search_mcp/safe_download.py",
        old=(
            "            target = parse_target(current, self.policy)\n"
            "            # 把**当前**这一跳记进 visited。不记的话，一个指回起点的重定向\n"
            "            # 要等到再发一次请求之后才被发现（复核报告 B1）。\n"
            "            if target.display not in visited:\n"
            "                visited.append(target.display)\n"
        ),
        new="            target = parse_target(current, self.policy)\n",
        test=(
            "tests/test_url_and_reason_contract.py"
            "::test_redirect_to_itself_is_detected_without_a_second_request"
        ),
        why="起点不在 visited 里，自环要多发一次请求才被发现",
    ),
    # ------------------------------------------- 复核报告 B4/B5/B6
    BugInjection(
        name="cookie-secure-flag-off",
        path="src/image_search_mcp/credentials.py",
        old="                    secure=True,",
        new="                    secure=False,  # 注入：secure 标志关掉",
        test="tests/test_credentials.py::test_cookie_jar_only_sends_over_https",
        why="secure=False 时明文 http:// 也会带上凭据（Cookies.set 的默认行为）",
    ),
    BugInjection(
        name="env-proxy-mounts-not-stripped",
        path="src/image_search_mcp/credentials.py",
        old=(
            '    mounts = getattr(client, "_mounts", None)\n'
            "    if not mounts:\n"
            "        return ()\n"
            "\n"
            "    removed: list[str] = []\n"
            "    for key in list(mounts):\n"
            "        if mounts[key] is None:\n"
            "            continue\n"
            "        del mounts[key]\n"
            '        removed.append(getattr(key, "pattern", str(key)))\n'
            "    return tuple(removed)"
        ),
        new="    return ()  # 注入：不再拆继承来的代理挂载",
        test=(
            "tests/test_credentials.py"
            "::test_inherited_env_proxy_mounts_are_removed"
        ),
        why="httpx 在构造时就把环境代理挂进 _mounts，不拆的话带凭据的请求会走它",
    ),
    BugInjection(
        name="domain-table-missing-entry",
        path="src/image_search_mcp/credentials.py",
        old='    "Google": ("google.com", "google.co.jp"),',
        new='    "Google": ("google.com",),',
        test=(
            "tests/test_credentials.py"
            "::test_every_upstream_host_is_declared_or_explicitly_excused"
        ),
        why="域表少一项时凭据发不出去（功能坏但没人知道）；单向核验发现不了这个",
    ),
    # ------------------------------------------- MCP 协议层（isError / 结构 / 注解）
    BugInjection(
        name="tool-annotations-dropped",
        path="src/image_search_mcp/server.py",
        old=(
            "@mcp.tool(\n"
            '    title="以图搜图",\n'
            "    annotations=SEARCH_ANNOTATIONS,\n"
            "    output_schema=SEARCH_OUTPUT_SCHEMA,\n"
            ")"
        ),
        new=(
            "@mcp.tool(\n"
            '    title="以图搜图",\n'
            "    output_schema=SEARCH_OUTPUT_SCHEMA,\n"
            ")"
        ),
        test=(
            "tests/test_mcp_contract.py"
            "::test_search_tool_declares_read_only_and_open_world"
        ),
        why="不声明只读/外部访问，客户端只能把这个只读工具当危险操作处理",
    ),
    BugInjection(
        name="output-schema-dropped",
        path="src/image_search_mcp/server.py",
        old=(
            "@mcp.tool(\n"
            '    title="以图搜图",\n'
            "    annotations=SEARCH_ANNOTATIONS,\n"
            "    output_schema=SEARCH_OUTPUT_SCHEMA,\n"
            ")"
        ),
        new=(
            "@mcp.tool(\n"
            '    title="以图搜图",\n'
            "    annotations=SEARCH_ANNOTATIONS,\n"
            ")"
        ),
        test=(
            "tests/test_mcp_contract.py"
            "::test_search_tool_declares_the_output_schema_we_promised"
        ),
        why="没有 outputSchema，客户端就只能靠解析文本，等于结构化输出承诺是空的",
    ),
    BugInjection(
        name="tool-error-returned-as-normal-result",
        path="src/image_search_mcp/server.py",
        old=(
            "    outcome = await _search_image_logic(source, engine, extra_params_json, limit)\n"
            "    if outcome.is_error:\n"
            "        # 工具执行错误 → 按 MCP 规范用 isError=true 交回给调用方，让它/模型能自我修正。\n"
            "        # 用 ToolError 而不是让裸异常冒出去：后者的文本会被加上\n"
            '        # 「Error calling tool \'search_image\': 」前缀（实测），对模型只是噪音。\n'
            "        raise ToolError(outcome.text)\n"
            "    return ToolResult(content=outcome.text, structured_content=outcome.structured)"
        ),
        new=(
            "    outcome = await _search_image_logic(source, engine, extra_params_json, limit)\n"
            "    # 注入：把失败当正常结果返回\n"
            "    return ToolResult(content=outcome.text, structured_content=outcome.structured)"
        ),
        test="tests/test_mcp_contract.py::test_input_rejections_come_back_as_is_error",
        why="失败退化成「看起来正常的结果」，客户端与模型都无法区分",
    ),
    BugInjection(
        name="bare-exception-instead-of-tool-error",
        path="src/image_search_mcp/server.py",
        old="        raise ToolError(outcome.text)",
        new="        raise ValueError(outcome.text)  # 注入：用裸异常",
        test=(
            "tests/test_mcp_contract.py"
            "::test_error_text_does_not_gain_a_tool_name_prefix"
        ),
        why="裸异常的文本被 fastmcp 加上「Error calling tool 'x': 」前缀，报错不再是我们写的那句",
    ),
    BugInjection(
        name="structured-content-dropped",
        path="src/image_search_mcp/server.py",
        old="    return ToolResult(content=outcome.text, structured_content=outcome.structured)",
        new="    return ToolResult(content=outcome.text)  # 注入：不给结构化内容",
        test=(
            "tests/test_mcp_contract.py"
            "::test_successful_search_returns_structured_content_and_keeps_the_text"
        ),
        why="声明了 outputSchema 却不给 structuredContent，客户端只能回去解析文本",
    ),
    BugInjection(
        name="no-results-marked-as-error",
        path="src/image_search_mcp/server.py",
        old=(
            "        return SearchOutcome(\n"
            '            text="\\n".join(lines),\n'
            "            structured=_structured_results(engine, [], limit),\n"
            "        )"
        ),
        new=(
            "        return SearchOutcome(\n"
            '            text="\\n".join(lines),\n'
            "            structured=_structured_results(engine, [], limit),\n"
            "            is_error=True,  # 注入：把「没结果」也标成错误\n"
            "        )"
        ),
        test="tests/test_mcp_contract.py::test_no_results_is_not_an_error",
        why="把「没搜到」报成失败，会让模型去修一个并不存在的问题（过度报错也是错）",
    ),
    # --------------------------------------------- 全仓复核批（P1，2026-10-04）
    BugInjection(
        name="upstream-explicit-error-treated-as-no-results",
        path="src/image_search_mcp/server.py",
        old=(
            "    upstream_error = _upstream_error(engine, response)\n"
            "    if upstream_error is not None:\n"
            "        return SearchOutcome(\n"
            '            text=f"Error: {engine} 明确报错 —— {upstream_error}",\n'
            "            is_error=True,\n"
            "        )\n"
        ),
        new="    # 注入：上游明确报错被忽略 —— 继续走「空 raw = 成功空结果」\n",
        test=(
            "tests/test_search_flow.py"
            "::test_upstream_explicit_error_is_not_reported_as_no_results"
        ),
        why=(
            "上游把失败写进 error/status、结果为空时，明确失败被报成「没搜到」"
            "（复核报告第 3 问，曾用真 TraceMoeResponse 复现 isError=false）"
        ),
    ),
    BugInjection(
        name="http-status-error-ignored",
        path="src/image_search_mcp/server.py",
        old="    if isinstance(code, int) and not isinstance(code, bool) and code >= 400:\n",
        new="    if False:  # 注入：上游 HTTP 4xx/5xx 被忽略\n",
        test=(
            "tests/test_search_flow.py"
            "::test_http_error_status_is_an_error_even_with_an_empty_body"
        ),
        why="HTTP 层报错被当成成功空结果 —— 与上一条同属「失败被报成没结果」",
    ),
    BugInjection(
        name="response-contract-error-treated-as-no-results",
        path="src/image_search_mcp/server.py",
        old=(
            '    if response is None or not hasattr(response, "raw"):\n'
            "        return SearchOutcome(\n"
            "            text=(\n"
            '                "Error: 上游响应缺少 raw 字段"\n'
            '                f"（{type(response).__name__}）—— 这是响应契约错误，"\n'
            '                "不能当成\'没有结果\'。"\n'
            "            ),\n"
            "            is_error=True,\n"
            "        )\n"
        ),
        new=(
            '    if response is None or not hasattr(response, "raw"):\n'
            "        # 注入：契约错误被当成「没结果」\n"
            "        return SearchOutcome(\n"
            '            text="No results found.",\n'
            "            structured=_structured_results(engine, [], limit),\n"
            "        )\n"
        ),
        test=(
            "tests/test_search_flow.py"
            "::test_response_without_raw_is_a_contract_error"
        ),
        why="响应形状不对（缺 raw / raw 为 None）时当成「没搜到」，掩盖适配层契约破裂",
    ),
    BugInjection(
        name="proxy-error-echoes-secret",
        path="src/image_search_mcp/credentials.py",
        old='            f"{PROXY_VAR} 缺少主机名：{_described_proxy(raw)}",\n',
        new='            f"{PROXY_VAR} 缺少主机名：{raw!r}",\n',
        test="tests/test_credentials.py::test_proxy_error_does_not_echo_the_secret",
        why=(
            "错误代理 URL 回显 secret：CredentialError 会经 server 的错误出口回给调用方"
            "（cookie 同类路径当初已修，代理这条漏了 —— 修一个实例没修一类）"
        ),
    ),
    BugInjection(
        name="duplicate-auth-header-last-wins",
        path="src/image_search_mcp/main.py",
        old=(
            "        duplicated = [\n"
            "            name\n"
            '            for name in (b"authorization", b"origin")\n'
            "            if sum(1 for key, _ in raw_headers if key.lower() == name) > 1\n"
            "        ]\n"
        ),
        new="        duplicated: list[bytes] = []  # 注入：重复头检查被去掉\n",
        test=(
            "tests/test_auth_asgi.py"
            "::test_duplicate_authorization_headers_fail_closed"
        ),
        why=(
            "重复 Authorization 头由头顺序决定放行：dict() 是后者覆盖前者，"
            "实测「无效在前、有效在后」→ 204 放行（复核报告第 12 问）"
        ),
    ),
    # ---------------------------------------------------------------- 复核 P1
    BugInjection(
        name="read-bounded-deadline-check-removed",
        path="src/image_search_mcp/safe_download.py",
        old=(
            "            if time.monotonic() > deadline:\n"
            '                raise DownloadError("timeout", "下载总期限已到", url=target.display)\n'
        ),
        new="            # 注入：内层期限检查被去掉\n",
        test=(
            "tests/test_safe_download.py"
            "::test_read_bounded_checks_the_deadline_itself"
        ),
        why=(
            "内层期限检查被删掉：忙转的流能远超总期限才被外层 wait_for 截停"
            "（复核实测 20 ms 预算跑了 135 ms）。我先前以为两层互相遮挡就删了它，删错了"
        ),
    ),
)


def by_name(name: str) -> BugInjection:
    for injection in INJECTIONS:
        if injection.name == name:
            return injection
    raise KeyError(f"没有这条注入：{name}")
