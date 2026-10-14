评审基准：本地提交 09dda2e；复用了已有测试环境，依赖版本与你列出的完全一致。另核对了 PyPI 0.2.1 的 wheel 和 sdist：两个核心文件与仓库逻辑一致，wheel 只有 CRLF 换行差异。

未修改、新建、删除文件，未访问生产服务。下面的动态验证均使用内存探针、模拟 HTTP 响应或进程内 ASGI 请求；没有读取真实凭据。下文 server.py、main.py 均指 src/image_search_mcp/ 下的文件。

1. A／B／C 复核

A：成立，属于必须修的功能故障；但“只要设置变量就 100% 失败”需要限定。

server.py:264–271 把字符串转成 dict，随后在 server.py:293 传给 Network。实测：

    IMAGE_SEARCH_COOKIES=k=v; k2=v2
    → 'dict' object has no attribute 'split'

    IMAGE_SEARCH_PROXY=http://127.0.0.1:7890
    → 'dict' object has no attribute 'url'

    HTTP_PROXY=http://127.0.0.1:7890
    → 同样失败

直接传字符串的 Network 构造成功，得到正确 cookies，HTTPX 显示 Timeout(timeout=30)。

需要修正两处表述：

- 空字符串不会进入配置分支；没有任何 k=v 的 cookie 字符串会被解析成空 dict，底层可能直接忽略。正常、有效的非空 cookie 配置必然触发你说的故障。
- 错误不只是“含糊的一句话”。server.py:338 还拼接了完整 traceback，实测确实返回给调用方。它同时存在错误处理和信息暴露问题。

这是当前正常使用中最容易撞到的问题，系统原本已有 HTTP_PROXY 的用户甚至不需要主动配置本项目。

B：成立，但不是当前主路径的启动故障。

两条导入都实测得到 ModuleNotFoundError：

    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp import create_sse_app

位置：server.py:8–11、main.py:60–65。

fastmcp 是项目声明的必需依赖，当前正常安装会走独立 FastMCP，因而这些分支主要是失效的兼容代码。server.py 的宽泛 ImportError 捕获还可能把 FastMCP 内部缺依赖的问题误判成“需要旧 SDK”。

建议删除没有测试支撑的兼容分支，明确支持范围。不要直接加 mcp<2：实测 FastMCP 4.0.10 的 server 依赖要求 mcp>=2.0.0,<3.0.0，会发生冲突。

C：成立，而且严重性高于“客户端连接不上”。

main.py:70 把 Streamable HTTP 放在 "/"；main.py:15–17 又无条件免认证放行 "/"，不区分方法。

首页 Route("/", homepage) 只处理 GET/HEAD，挡不住 POST。Starlette 会继续匹配后面的根路径 MCP 路由。

我强制让 SSE 应用构造抛异常，保留其余真实 FastMCP、Starlette 和认证代码，得到：

    FALLBACK_ROUTES [
      ('Route', '/', ['GET', 'HEAD']),
      ('Route', '/', None)
    ]
    FALLBACK_ANON_INITIALIZE 200 True
    FALLBACK_ANON_TOOL_CALL 200 True isError_false= True

最后一项是未携带 token 的 get_engine_info 工具调用，不只是访问到健康页。

结论：这个 fallback 会产生条件性的认证绕过。触发前提是启动时 SSE 构造失败；没有证据证明公网请求能够主动触发，也不能据此声称你的现网现在已经匿名开放。

严重性排序：

- 安全影响：C 最高，A 次之，B 最后。
- 当前正常配置的故障概率：A 最明确；C、B 都依赖异常分支。
- 正确修法是应用构造失败就退出，不能静默改变协议、端点和认证覆盖范围。

2. 补充发现

2.1 高风险：extra_params_json 可以重新启用服务器本地文件读取

server.py:312 的 search_kwargs.update(extra_params) 可以覆盖前面准备好的 file、url。

工具描述虽然禁止本地路径，但这只是提示词。PicImageSearch 的 file 参数本来就接受路径字符串：

- PicImageSearch/engines/yandex.py:75–81：读取 file，然后上传。
- PicImageSearch/utils.py:47–78：字符串路径最终进入 open(file, "rb").read()。

离线验证中，用模拟读取函数代替真实文件访问：

    FILE_OVERRIDE call('/synthetic-review-canary')
    → POST https://yandex.com/images/search...
    → multipart 请求包含 SYNTHETIC_CANARY

这证明攻击参数能到达本地文件读取分支，且读取结果会被上传。没有读取任何真实敏感文件。

影响边界：

- 正常情况下需要通过 MCP 认证；若 C 的降级发生，则可能变成匿名可调用。
- 已证明的是“读取进程可读文件并上传给第三方搜索服务”，不是“原始文件必然直接回显给调用者”。
- 生产以 root 运行显著扩大了危害；有 token 的 LLM 工具调用也不应拥有任意文件读取权限。

修复：按引擎建立参数白名单和严格类型校验，禁止 extras 携带 file、url 等保留键。不要只禁止某几个危险路径。

2.2 高风险：URL 下载存在 SSRF；修好 cookies 类型后还有跨域泄露风险

server.py:300–315 只检查 http/https 前缀，没有目的地址约束。

不能说所有引擎都会在本机抓图，但至少以下两个会：

- EHentai：PicImageSearch/engines/ehentai.py:89–90。
- BaiDu：PicImageSearch/engines/baidu.py:98–99。

真实引擎加 MockTransport 捕获到：

    LOCAL_URL_DOWNLOAD EHentai GET http://127.0.0.1:9999/synthetic-image
    LOCAL_URL_DOWNLOAD BaiDu   GET http://127.0.0.1:9999/synthetic-image

没有实际连接回环服务。底层 Network 还设置了 follow_redirects=True，见 PicImageSearch/network.py:65。因此只检查初始 URL 不够。

更容易遗漏的是 cookie：

PicImageSearch/network.py:50–60 把配置转成无域限制的 cookie 字典，交给 HTTPX。我用虚构 cookie 构造发往任意域名的请求，实际得到：

    GET https://untrusted.invalid/image
    Cookie: sid=SYNTHETIC_COOKIE

当前错误的 dict 传参阻止了正常 cookie 使用；单独修好 A，会暴露这个原本被故障遮住的问题。

建议一起修：

- 引擎 cookie 按引擎及可信域名限定。
- 用户图片 URL 下载使用独立、无登录凭据的 client。
- 校验实际解析和连接的地址，覆盖 IPv4、IPv6、回环、私网、链路本地地址及每次重定向。
- 处理 DNS 重绑定，避免“校验一次 DNS，连接时重新解析”。
- 配合出站网络限制；不能仅靠字符串黑名单。

2.3 公网部署的边界需要收紧

明文 HTTP

main.py:100 没有启用 TLS；部署脚本 :129、:149 明确使用 HTTP。按你描述的公网直达方式，远程客户端若直连这个地址，Bearer 会在明文链路上传输。强随机 token 不解决窃听问题。

若调用方只有同机 SillyTavern，优先绑定回环地址；确有远程需求，则使用 TLS 入口，并关闭公网对明文后端的直接访问。

缺 token 时静默开放

main.py:94–97 只在环境变量非空时安装中间件。实测在 --host 0.0.0.0 且没有 token 的情况下，服务照常启动，匿名请求已到达 MCP 消息处理器。

生产当前配置了 token，不能把它描述成现存无认证；问题是下一次配置遗漏会“成功启动但完全开放”。

建议 HTTP 模式缺少有效 token 时拒绝启动；无认证仅允许显式选择的本地开发模式。空白 token 也应在启动时判错。

root

root 本身不是远程漏洞，但会放大文件读取、依赖漏洞和配置错误。应改专用低权限用户，配合 NoNewPrivileges、文件系统访问限制和资源限额。上线前验证这些限制不影响证书、DNS、依赖加载和搜索。

2.4 Origin 校验缺失，有源码和动态依据

main.py 没有 Origin 校验。当前 FastMCP SSE 工厂创建 SseServerTransport 时未传安全配置，见 fastmcp/server/http.py:443；mcp/server/transport_security.py:46–48 在未配置时关闭重绑定防护。

携带有效测试 token 和 Origin: https://evil.invalid，POST /messages/ 返回：

    400 session_id is required

说明请求已经越过安全检查进入协议处理，而不是因恶意 Origin 被拒绝。

旧 HTTP+SSE 规范也要求校验 Origin，不能以“还是旧 SSE”免除。应显式设置允许的 Origin，并测试合法客户端不带 Origin 的情况；不要把 CORS 响应头当成服务端校验。

2.5 超时和资源：有底层超时，没有整个工具调用的期限

“每个引擎完全没有 timeout”不成立。

PicImageSearch/network.py:33、:64 默认 timeout=30，HTTPX 实测也是 Timeout(timeout=30)。但这不是整个 search 的总期限：

- 一次搜索可能发起多个请求。
- HTTPX 读超时不是累计下载时长限制；持续收到数据可能一直延长总耗时。
- TraceMoe 还会并发补充番剧资料，见 engines/tracemoe.py:228。
- server.py:315 没有外层总 deadline。
- 没有应用级并发上限、排队上限或配额保护。

建议提供请求阶段超时和工具调用总期限两层限制，再加有界并发。项目声明支持 Python 3.10，直接使用 asyncio.timeout 会不兼容；可以使用 asyncio.wait_for 或合适的 AnyIO 超时机制。

资源泄漏方面，不应误报：

- server.py:293 使用 async with Network。
- PicImageSearch/network.py:88–95 在退出时关闭 client。
- 实测正常退出后 client.is_closed=True。
- 取消探针也进入了 __aexit__，CancelledError 未被 except Exception 吞掉。

没有发现普通成功、异常路径的明显 client 泄漏。每次新建 client 会失去跨调用连接复用，但这是性能取舍，不是泄漏；共享 client 又需要妥善隔离 cookie。

2.6 输入、limit 和返回大小存在真实缺陷，但不能无限夸大

limit

server.py:321–323 直接 min 和切片，实测：

    LIMIT -1 Found 3 results (showing top -1): actual_items=2

超大 limit 不会凭空分配对应数量的结果；但项目没有稳定输出预算，而且 limit 只限制最后展示，不能限制上游下载、解析和额外查询。即使 limit=0，也已经完成了搜索。

建议限定范围，例如 1–20，并为输出字符串长度、单条字段长度设预算。

JSON

server.py:255 只保证能 json.loads，不保证是对象：

- null、[1] 产生内部异常和 traceback。
- [["file", "..."]] 会被 dict.update 当成键值对接受。

必须在创建 Network 前验证顶层类型、键名、字段类型和取值范围。

Base64

server.py:304–307 对任意逗号切分，而且 b64decode 未启用严格校验。实测 "!!!!" 被接受并传成 file=b""。

应明确支持普通 Base64 和合法 data URI，验证非空、编码、允许的图片类型及解码后大小。

请求大小

不能说当前 HTTP 请求体完全无限制：mcp 2.3.0 的 SSE 消息端点默认限制 4 MiB，本次实测超限得到 413。依据：mcp/server/transport_security.py:15、sse.py:134。

不过这个 SDK 限制不能代替图片业务限制，也不限制引擎从 URL 下载的响应大小。PicImageSearch/network.py:332–333 会读取完整响应。

2.7 错误在协议层被当成成功，且泄露内部细节

server.py:257、:261、:310、:335–338 都返回字符串；没有表达 MCP 工具执行失败。

真实 FastMCP Client 调用不存在的引擎，结果是：

    TOOL_FAILURE_ISERROR False
    content= Error: Unsupported engine 'NotAnEngine'...

应使用 SDK 支持的工具错误机制或显式 isError=true，而不是只在文本前面加 Error。

traceback 会暴露模块路径、依赖栈和异常细节；异常信息还可能包含敏感 URL 或配置片段，但不能据此断言 token 已经泄露。客户端应收到稳定错误码、可操作的简短描述和关联 ID，详细堆栈留在脱敏后的服务端日志。

另外，server.py:327–331 把真正的空结果、配额不足、上游错误和 CAPTCHA 混成一类。应检查引擎特有的 status/status_code/error，不能一律建议加 cookie。

2.8 认证细节的实际严重性

非常量时间比较

main.py:29 使用 ==，确实应改为 secrets.compare_digest。属于成本低的加固，不应把它排在已经证实的文件读取、SSRF 和降级绕过之前。没有证据证明能从公网实际恢复这个 64 hex token。

畸形请求头

main.py:21 强制 UTF-8 解码，非法字节会抛异常。实测 Authorization 含非法字节时返回 500。应拒绝为 400/401，而不是制造内部错误。

WebSocket

中间件确实不认证 websocket，但当前没有 WebSocket 工具路由，不能直接宣称可以绕过认证调用搜索：

- /sse、/mcp 的匿名 WebSocket scope 被关闭。
- /messages/ 会进入 HTTP 专用处理器并触发 KeyError: 'method'。

建议对不支持的 WebSocket 明确拒绝，同时保持 lifespan 正常透传。

根路径

正常 SSE 下公开 GET "/" 只是暴露服务名称和端点，风险很低，可以保留简化健康检查。问题是“不分方法地豁免整个根路径”。改成独立 /healthz，仅允许 GET/HEAD，更不容易与将来的 MCP 路由冲突。

401 还应补 WWW-Authenticate: Bearer，改善标准 HTTP 认证行为。

2.9 已经失效的参数说明和结果字段

- server.py:65 公示 TraceMoe 的 cutBorders，但真实 Python 参数是 cut_borders。实测传 cutBorders=false，上游请求仍包含 cutBorders=true。
- server.py:41–42 公示 Yandex 的 rpt、cbir_page，但依赖内部硬编码这两个值，额外 kwargs 被忽略。实测覆盖无效。
- server.py:52、:278 开放 SauceNAO output_type，而依赖 engines/saucenao.py:140 固定 json.loads；不应允许调用方选 HTML/XML 后再当 JSON 解析。
- server.py:177–179 仅给 Iqdb 输出 similarity，SauceNAO 和 TraceMoe 的置信度被丢掉。
- TraceMoe 的 image、video 未映射；episode=0、To=0 会受真值判断影响而消失或显示成问号，见 server.py:190–197。
- get_engine_info 不区分引擎名大小写，search_image 却区分，见 server.py:143、:259。应统一或用明确枚举约束。

2.10 部署脚本还有独立问题

image_search_mcp_deploy.sh:51 会 source .env，但 :70–80 又把用户输入未经转义写进去。

普通 cookie 已能出错。内存 shell 探针：

    IMAGE_SEARCH_COOKIES=k=v; k2=v2
    → cookie=k=v secondary=v2

再次 source 时，分号被解释成 shell 语法，cookie 被截断。包含命令替换的内容还可能执行命令。注意：这是 shell source 的问题，不是 systemd EnvironmentFile 自己会执行 shell。

此外，PORT 未校验就进入 sudo bash -c 的第二层 shell，见 :119–137。应把配置当数据解析，校验端口，避免将输入拼接进 shell 程序。

脚本管理的是 image-search.service（:37、:119、:142），而你提供的生产单元是 image-search-mcp.service。这个脚本不能原样作为现网更新入口。

3. MCP 规范一致性与取舍

3.1 HTTP 模式应以 Streamable HTTP 为新默认，但不要改变 stdio 默认或偷偷破坏老客户端

官网当前协议版本为 2026-07-28。HTTP+SSE 自 2025-03-26 被 Streamable HTTP 替代；当前规范明确建议新实现不要采用旧传输，已有实现应迁移。

“旧 HTTP+SSE 过时”不意味着“SSE 编码被禁止”：Streamable HTTP 仍可用 SSE 返回响应流。

建议：

- CLI 无参数继续走 stdio。
- 增加明确的 HTTP 传输选项，现代端点使用 /mcp。
- --sse 保留为兼容模式，迁移前实测 SillyTavern。
- 必要时同时暴露两套端点，并让认证覆盖全部端点。
- 构造失败直接退出，禁止异常驱动的协议降级。

现行规范还改变了 Streamable HTTP 的 GET 流和会话机制。因此应声明并测试实际支持的协议版本，不能只改 transport 字符串就宣称完整支持最新版。

依据：
https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http

3.2 建议输出有业务含义的结构化结果，但纯文本本身并不违规

当前 FastMCP 4 会为 str 返回值自动生成 result:string 的 outputSchema。因此严格说，不是“一定完全没有 structuredContent”；问题是结构化外壳里仍是一大段难以可靠解析的展示文本。

建议定义稳定对象，包含：

- engine、查询状态。
- retrieved_count、returned_count、truncated；不要把当前页结果数冒充全网总数。
- results：title、url、thumbnail、similarity 等通用字段。
- 少量经过白名单筛选的引擎专属字段。
- warnings。

收益是客户端可以排序、去重、渲染和处理错误，不需要让 LLM 从英文标签里猜字段。

代价是需要维护 schema、处理不同引擎的差异；同时输出 JSON 和长篇人类文本还会增加 token。建议以结构化对象为主，并按规范建议提供序列化 JSON 文本副本兼容旧客户端，不再另外拼一份冗长报告。

结构化输出不会自动消除搜索标题里的提示注入，调用方仍需把内容视为外部数据。

依据：
https://modelcontextprotocol.io/specification/2026-07-28/server/tools

3.3 固定 Bearer 可以继续用于这个自托管场景

它是自定义访问控制，不等于完整 MCP OAuth。不能仅因没有 OAuth 就说所有 MCP 功能不合规，但也不能承诺标准 OAuth 客户端的自动发现、登录互操作。

就当前单用户工具服务而言，先修认证覆盖、TLS、token 轮换和最小权限，比立即引入 OAuth 服务更实际。

4. 打包与依赖

4.1 应区分“发布包兼容范围”和“生产环境锁定”

pyproject.toml:15–19 全无版本范围不违反包装格式，但这个项目直接适配多套快速变化的 API，不声明范围会把兼容性测试外包给用户。

建议：

- pyproject.toml 声明实际验证过的最低版本和有依据的上界。
- FastMCP 4 是目前已验证的主路径；若不准备测试旧版，就不要保留伪兼容。
- 生产部署另外锁定直接及传递依赖，形成可重建环境。
- 仅固定 image-search-mcp==某版本，仍然没有锁住依赖。
- 使用固定环境或带完整锁定约束的运行方式，避免生产重建时临时解析一套新依赖。

不建议把整套传递依赖的精确版本全部塞进 PyPI 包 metadata，会不必要地限制其他用户环境。

4.2 requirements.txt 当前没有冲突，但存在双份维护风险

requirements.txt:1–3 与 pyproject.toml 重复声明。建议让 pyproject.toml 成为直接依赖的唯一来源；部署锁文件由它生成。若保留 requirements 文件，应明确它是生成的部署清单或开发入口，不再手抄另一份依赖列表。

main.py:76–77 直接使用 Starlette，应该显式声明依赖；若采用 Pydantic/AnyIO 编写验证或超时逻辑，也应声明自身直接使用的依赖。

4.3 LICENSE 和作者占位符已经进入真实发行物

PyPI wheel 核对结果：

    Author-email: Your Name <your.email@example.com>
    License: MIT
    License-Expression: 缺失
    License-File: 缺失

wheel、sdist 都没有 LICENSE 文件。

这不会必然阻止上传 PyPI，但属于真实发行质量问题：用户拿不到完整授权文本和版权声明，自动化合规工具也难以验证。

建议：

- 添加正确版权署名的 MIT 正文。
- 使用 license = "MIT"、license-files = ["LICENSE"]。
- 同步提高构建后端最低版本；Setuptools 从 77.0.0 支持这些 PEP 639 写法。
- 作者改真实署名；邮箱可省略，不必公开私人邮箱。
- 添加仓库和问题反馈 URL。
- 构建后检查 wheel、sdist 内容，不只检查源码目录。

不要把旧 license table 与新 license-files 随意混用。

参考：
https://peps.python.org/pep-0639/
https://raw.githubusercontent.com/pypa/setuptools/main/docs/userguide/pyproject_config.rst

5. 不依赖真实外网的测试方案

核心原则：模拟搜索站点，不要把正在验证的适配边界也全部模拟掉。

建议结构如下，仅为设计，未创建文件：

    tests/
      conftest.py
      unit/
        test_inputs.py
        test_parameters.py
        test_results.py
        test_error_mapping.py
      contract/
        test_network_config.py
        test_engine_requests.py
        test_url_security.py
      integration/
        test_auth_asgi.py
        test_transport_selection.py
        test_mcp_stdio.py
        test_mcp_http.py
        test_timeouts_and_cleanup.py
      packaging/
        test_distribution.py
      fixtures/
        各引擎最小 HTML/JSON 响应

5.1 环境与网络隔离

- 清除搜索凭据、大小写代理变量等环境污染。
- 默认禁止真实网络；HTTP 响应走 HTTPX MockTransport。
- 完整 HTTP/SSE 测试只允许受控回环连接，不允许访问外网。
- fixture 使用明确标注的合成响应，不包含真实 cookie、token、签名 URL。

5.2 cookie/proxy 回归必须使用真实 Network

test_network_config.py：

- 使用真实 PicImageSearch.Network 和 HTTPX，验证合法 cookie、proxy 字符串能够构造、正确解析并关闭。
- 验证应用层实际传入的是字符串，而不是只测试一个孤立 parser。
- 检查环境变量优先级、空值和畸形 cookie 的错误处理。
- 模拟传回 dict 的回归，确保测试确实失败。

如果把 Network 整体替换成宽松 MagicMock，这个测试很可能永远发现不了 A。

5.3 参数和出站请求契约

test_inputs.py、test_parameters.py：

- JSON 语法错误、null、数值、数组、键值对数组。
- file/url 保留键、未知键、错误布尔值和数值范围。
- limit 的负数、零、合法边界、超大值。
- 非法 Base64、空数据、data URI、解码后超限。
- 引擎名称大小写策略。

test_engine_requests.py：

- 真实引擎类配 MockTransport，逐一核对已公布参数是否真的改变请求。
- TraceMoe cut_borders=false 不应发 cutBorders=true。
- Yandex 不应公示不起作用的参数。
- SauceNAO 不接受不兼容的 output_type。
- URL 和上传输入分别覆盖；不能只测默认 Yandex。

5.4 文件、SSRF 和 cookie 安全回归

test_url_security.py：

- extras.file 在调用任何读取函数前被拒绝。
- 回环、私网、链路本地、IPv6 和公网重定向到私网被拒绝。
- DNS 校验结果与实际连接目标保持一致。
- 图片下载请求不携带引擎 cookie。
- 引擎 cookie 不会发给其他引擎域名。
- 下载超过大小预算会及时中止。

只禁止字符串 "127.0.0.1" 的实现必须在这些测试中失败。

5.5 认证和传输必须测试真实装配后的应用

test_auth_asgi.py：

- /sse、/messages/、/mcp 的缺失、错误、正确 token。
- Bearer 大小写、允许的空白、非法编码、重复 Authorization。
- 健康端点只允许规定方法；其他方法不能继承免认证权限。
- WebSocket 明确拒绝，lifespan 正常透传。
- 非法 Origin 被拒绝；合法原生客户端不带 Origin 可以工作。
- 缺 token 的公网模式启动失败。

test_transport_selection.py：

- 强制 SSE 工厂抛异常，断言进程失败、uvicorn.run 没有被调用。
- 明确选择 SSE/HTTP 后，实际路径和协议对应。
- 不允许只看到根路径 200 就算启动成功。

5.6 真跑 MCP 协议和资源生命周期

- stdio 使用子进程，完成协议交互，确认 stdout 没有日志污染。
- HTTP 使用本地测试服务器和真实 SDK 客户端；旧 SSE 验证 GET 流与 POST 消息端点都要求认证。
- 完成工具发现、调用成功、参数错误、上游错误，断言 isError 和输出 schema。
- 用挂起的模拟引擎验证总 deadline、取消传播、资源释放及并发上限。
- HTTPX MockTransport 不应被当成真实 socket 超时的验证器；connect/read timeout 用受控的本机延迟服务测试。

5.7 输出和发行物测试

- 每个引擎的关键字段、空结果、上游错误和限额状态。
- similarity、TraceMoe 时间零值、预览链接、实际返回计数。
- 输出没有 traceback、虚构凭据标记或内部路径。
- 构建 wheel/sdist，在隔离环境安装后测试入口，不只跑 editable 源码。
- 检查 LICENSE、元数据、依赖一致性；执行 pip check。
- Python 3.10 和其他声明支持版本，以及最低依赖组合、部署锁定组合都进 CI。

还有一个现成陷阱：.gitignore:33–36 忽略 test_logic.py、integration_test.py 等文件。我运行 git check-ignore，连 tests/test_logic.py 都被忽略。应移除这些针对正式测试名称的规则，否则新测试可能根本没提交。

6. 按优先级排序的改进清单

必须立刻修：安全暴露、正常功能损坏，以及阻止它们回归的门禁。

优先级  文件／位置
        改什么；为什么；怎么验证

P1-1    main.py:15–17、:60–72
        删除静默协议降级；健康检查改独立路径并限制方法。
        防止条件性匿名 MCP 调用。
        强制 SSE 构造失败必须退出；匿名 POST 不能进入任何 MCP 路由。

P1-2    server.py:251–315
        严格校验 extras 对象、按引擎白名单传参、禁止 file/url 覆盖。
        阻断服务器文件读取和输入边界绕过。
        路径覆盖探针必须在读取函数和出站请求前失败。

P1-3    server.py 网络适配层、README.md:89–94
        增加安全图片下载；限制目标地址、重定向和大小；隔离引擎 cookie。
        阻断 SSRF，以及修复 A 后出现的 cookie 跨域发送。
        私网和重定向测试被拦截，任意图源请求没有 Cookie 头。

P1-4    server.py:104–122、:264–271；对应配置文档
        按 Network 契约传字符串，并验证配置格式。
        修复正常配置导致全部搜索失败的问题。
        使用真实 Network 验证 cookie/proxy；让旧 dict 实现明确测试失败。

P1-5    main.py:94–100；部署脚本及实际生产单元
        缺有效 token 拒绝公网启动；同机调用绑定回环；远程入口用 TLS；
        使用专用低权限用户，统一实际服务名称。
        消除配置遗漏开放、明文凭据和 root 放大风险。
        验证启动拒绝、端口暴露、运行 UID，以及真实客户端可用性。

P1-6    main.py:6–45；HTTP 应用装配
        加 Origin 校验，稳健处理认证头，使用 compare_digest，
        明确拒绝 WebSocket，补认证挑战头。
        修复协议安全缺口和畸形请求 500。
        跑完整认证矩阵，恶意 Origin 不进入消息处理器。

P1-7    server.py:293–338
        加总期限、有界并发、输入／下载／输出预算；
        工具错误使用 isError，客户端不返回 traceback。
        控制资源消耗和错误信息暴露。
        超时、取消、过载、脱敏和资源关闭测试通过。

P1-8    pyproject.toml、生产锁定清单、.gitignore、tests/
        声明受测依赖范围，部署锁定传递依赖；
        让上述回归测试进入版本控制及 CI。
        避免重新安装即改变行为、修过的问题再次发布。
        隔离安装真实 wheel 后运行离线测试与 pip check；
        故意恢复错误实现时，相应测试必须失败。

P1-9    image_search_mcp_deploy.sh:51、:70–80、:119–137
        不再 source 数据配置，不拼接输入到第二层 shell，严格校验端口。
        修复正常 cookie 截断和管理入口命令执行风险。
        多项 cookie 可完整往返；命令替换文本不执行；非法端口被拒绝。

值得做：按下一次受测发行推进，不要与安全修复绑定成大重构。

P2-1    server.py:37–100、:164–235、:273–290
        修正参数说明及映射，补相似度、预览和零值字段。
        避免参数静默无效、关键搜索信息丢失。
        真实引擎请求契约测试及各引擎结果测试通过。

P2-2    main.py、README.md
        新 HTTP 使用明确的 Streamable HTTP /mcp，保留显式旧 SSE 兼容。
        跟进规范，同时避免破坏 SillyTavern。
        按声明支持的协议版本和客户端分别完成端到端测试。

P2-3    server.py 的工具签名和结果模型
        提供业务结构化结果及兼容 JSON 文本，不再仅包装展示字符串。
        方便可靠渲染、排序、计数和错误处理。
        验证 outputSchema、实际 structuredContent 和旧客户端文本内容。

P2-4    pyproject.toml、LICENSE、发行流程
        补完整 MIT 文本、真实署名、项目 URL，迁移 PEP 639 元数据。
        修复已经发布出去的许可与作者信息缺陷。
        检查新 wheel/sdist 的许可证、元数据，并执行 twine check。

P2-5    TODO.md、README.md、服务监控
        用真实 CI 结果替代“代码层面验证”；监控协议可用性而非仅根路径 200。
        避免服务看起来活着，实际协议错误或搜索全部失败。
        构造启动失败、认证失败、上游失败，确认监控能区分和告警。
ASTRA_EXIT=0
