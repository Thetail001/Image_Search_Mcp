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
            "        return (\n"
            '            f"Error: 搜索过程中发生内部错误（{type(exc).__name__}）。"\n'
            '            "详细信息见服务端日志。"\n'
            "        )"
        ),
        new='        return f"Error: 内部错误（{type(exc).__name__}）: {exc}"',
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
        old='            jar.set(name.strip(), value.strip(), domain=domain, path="/")',
        new="            jar.set(name.strip(), value.strip())  # 注入：去掉域限定",
        test=(
            "tests/test_credentials.py"
            "::test_scoped_jar_only_sends_to_the_engine_domain"
        ),
        why="凭据外发：无域 cookie 会跟着请求发往任何域名（实测过 sid 发给 untrusted.invalid）",
    ),
    BugInjection(
        name="download-total-deadline-removed",
        path="src/image_search_mcp/safe_download.py",
        old=(
            "            if time.monotonic() >= deadline:\n"
            "                raise DownloadError(\n"
            '                    "timeout",\n'
            '                    f"下载超过总期限 {self.policy.total_timeout}s（已读取 {total} 字节）",\n'
            "                    url=target.display,\n"
            "                )"
        ),
        new="            pass  # 注入：总期限检查被去掉",
        test=(
            "tests/test_safe_download.py"
            "::test_slow_trickle_is_stopped_by_total_deadline"
        ),
        why="慢速下载拖死连接：总期限只在跳与跳之间检查、读取循环里不检查",
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
)


def by_name(name: str) -> BugInjection:
    for injection in INJECTIONS:
        if injection.name == name:
            return injection
    raise KeyError(f"没有这条注入：{name}")
