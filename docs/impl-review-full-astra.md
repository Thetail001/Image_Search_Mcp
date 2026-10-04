# Image Search MCP 实现质量复核（全仓定向，一次）

状态：本轮定向复核完成；15 问均有结论或明确的未验证边界。结论是“核心加固有价值，但目前不应直接发布”，不是以问题数量否定整批实现。

## 范围与证据规则

- 工作树初始干净；实测分支 `audit/security-and-contracts`，HEAD `36f37bd`。任务书的“7 个提交”已经落后于当前 `git log --oneline -10`，本报告按实际 HEAD 复核，不改源码、不访问线上、不推送。
- 先读 `docs/实施进度.md` 与提交记录，按问题顺序定向检查，不通读历史审计报告。
- 区分源码确认、离线实测、外部规范核验、未验证；既有测试数量不作为本轮实测结果。
- 只允许修改本报告。探针与测试临时数据放运行环境指定的临时目录；关闭仓库内 bytecode/pytest 缓存写入。已授权的 check.sh 内部存在“改原部署脚本再还原”的副作用，运行后已确认还原，详见第 10 问；本轮未手动修改任何被审源码。
- 下文 `server.py` / `safe_download.py` / `credentials.py` / `params.py` / `main.py` 均指 `src/image_search_mcp/` 下同名文件；测试文件在 `tests/`，gate 与 reverse_tests 在 `tools/`。上游路径均取自本仓 `.venv/lib/python3.11/site-packages/`，不指上游 main。
- 实际基线距离：`git rev-list --count 09dda2e..HEAD` 返回 9。

## A. 最没把握的两个模块

### 1. safe_download.py 地址策略

结论：没有复现出在默认解析器/直连网络条件下，靠 URL 花式写法直达被拒内网的绕过；但发现一个确定的“访问主机与现代 URL 解释不同”的 IDNA 规范化问题。网络路由层的特殊翻译及本机公网地址也不在这层的保证里。不能把这个结论写成“已证明所有 SSRF 不可能”。

源码：`safe_download.py:102-159` 递归处理 IPv4-mapped/6to4，拒绝特殊网段及各类非公网标志；`280-305` 拒绝混合公网/内网 DNS 结果；`312-316,471-501` 用校验后的数字 IP 构造请求，原主机名只用于 Host/SNI；`377-419` 每跳重新校验、限跳并拒 HTTPS 降级。这些是实质有效的修复。

本轮离线地址探针（真实 parse/resolve/validate/fetch 路径，libc 解析数字别名且用 AI_NUMERICHOST 禁外部 DNS；HTTP transport 记录而不连接）：

- `file:///etc/passwd`、`ftp://127.0.0.1/a`、`http://x@127.0.0.1/a`：invalid_url，零 HTTP 请求。
- `http://2130706433/a`、`http://0177.0.0.1/a`、`http://0x7f000001/a`、`http://127.1/a`、`http://127.0.0.1./a`：blocked_address，零请求。
- `http://[::ffff:127.0.0.1]/a`、`http://[2002:7f00:1::]/a`、`http://[64:ff9b::7f00:1]/a`：blocked_address，零请求。
- `http://%31%32%37.0.0.1/a`：dns_failure；`http://１２７.０.０.１/a`：IDNA 规范化后 blocked_address；都未连接。
- 多跳 `http://1.1.1.1/a` → `http://1.0.0.1/b` → `http://2130706433/secret`：第三跳 blocked_address，只记录前两次公网请求。已有门禁同时覆盖混合 DNS、重绑定、降级和循环。
- `http://1.1.1.1//127.0.0.1/a` 仍请求 1.1.1.1，双斜线没有把主机换成内网。`http://1.1.1.1:22/a` 会被允许：端口只做 1–65535 合法性检查，不只允许 80/443（`223-226`）。

**确定的主机语义偏差：** 输入 `https://faß.de/a`，本层 `parse_target(...).host` 为 `fass.de`，httpx.URL 的原始主机为 `xn--fa-hia.de`。实测输出：`safe_downloader_host fass.de httpx_host xn--fa-hia.de`。原因是 `safe_download.py:230-240` 使用 Python 内置 IDNA 2003 编码，把 ß 映射为 ss；httpx 使用的现代 IDNA 不这么映射。这会解析并访问另一个可独立注册的公网主机，逐跳校验也只会检查已错误归一化的新名字，所以拦不住。不是已复现的内网 SSRF，但确实是不符合现代 URL 语义的目标变化。应统一到 httpx 使用的主机规范化算法，或者明确拒绝这种有偏差的主机名，增加两端一致性反例。

**环境相关的边界：** 当前策略允许本机公网地址、任何允许公网段上的 HTTP 端口，以及看起来全球单播的网络特定 NAT64 前缀。例如 `http://[2001:4860:64::7f00:1]/a` 的地址判定为 True；只有当部署网络实际把该 /96 配成通向 127.0.0.1 的翻译前缀时，才可能变成内网访问。这只是条件构造，没证实线上有这种路由，也没向该地址发请求。若威胁模型要求“不可访问本机任何接口/站点翻译出口”，需部署侧 egress 与明确拒绝本机地址/网络前缀；不能期待 ipaddress 推断路由配置。

不限制响应为实际图片、也不限制公网端口，是当前能力边界而非证实的绕过；是否限制要按用户图源兼容性决定，不能偷偷把“公网”解释成“图片 CDN 白名单”。

尺寸防线实现正确：`507-545` 先拒 HTTP 错误、超限声明长度、非 identity 编码，然后 `570-582` 流式累计实际字节。上限是“接受的 body 字节”，不是进程内存硬上限：chunks 与 join 同时存活，调用并发也没有统一上限。下载默认 8 MiB，Base64 输入默认 16 MiB（`safe_download.py:72`、`server.py:104`），不是同一个预算。

### 2. 并发与异步

有实质缺口：`server.py:584` 的 `await client.search(...)` 没有整次搜索总期限或响应体预算。`PicImageSearch 3.12.11/network.py:33, 58-66` 只有 httpx 的 30 秒 I/O timeout，不能阻止持续滴流；下载器的 15 秒只罩住本地抓图，不罩住搜索引擎访问。不要把“下载器有绝对期限”泛化成“工具调用有绝对期限”。

事件循环内存在同步工作和同步 IO：`Network.__init__` 同步创建 SSLContext（上游 `network.py:56-58`），httpx `_config.py:34-40` 会读取 CA 文件；SafeDownloader 每个地址/每跳的 AsyncClient 构造也在协程中（`safe_download.py:498`）。不是所有 AsyncClient 构造都“零阻塞”。Base64 解码、JSON/HTML 解析与 join 则是同步 CPU/内存工作（`server.py:368`、`safe_download.py:582`、上游 `network.py:256-257,299-308`）。不能为保持短代码宣称没有阻塞 IO。可复用不可变 SSLContext，并在容量测试后决定哪些 CPU 工作需 offload，别先共享带 cookie 的 client。

每次调用独立构造 `Network` 和引擎（`server.py:564-584`），没有跨调用共享同一个 client 的当前竞态；httpx 内部 cookies/headers 是可变状态，因此将来不能为了连接池复用而简单把它提成全局对象。当前没有自建信号量，也没有这条路径的临时文件，不存在它们的释放问题。

取消路径源码方向正确：CancelledError 不被 `except Exception` 吞掉；下载 finally 关 client（`safe_download.py:547-549`），搜索由 Network.__aexit__ 关闭（上游 `network.py:88-95`）。但 await 清理本身没有硬期限，外层 wait_for 会等取消完成；“失败后的清理也必须在 total_timeout 内结束”（`safe_download.py:355-356`）不成立。双重取消与真实 socket 释放尚未实测。

### 3. 失败语义

SearchOutcome 与协议 ToolResult/ToolError 分离是合理的，小数据类解决了真正存在的歧义（`server.py:113-125, 661-667`），不是应该删掉的抽象。

但“没搜到 = 成功”的前提被实现省掉了：`server.py:598-611` 用 `getattr(response, 'raw', None)` 的假值直接判成功，连 response=None、缺 raw 的异常响应、上游明确报错但 raw=[] 都会变成 No results found。成功且结果为空应成功；未能建立成功事实的响应不能仅凭空列表视作成功。

已用真实 TraceMoeResponse 类和完整 MCP 工具链复现：mock HTTP 200，响应体 `{"frameCount":0,"error":"synthetic upstream failure","result":[]}`；真实 TraceMoe 引擎解析后，`Client(server.mcp).call_tool_mcp("search_image", ...)` 返回 `is_error=False`，文本 `Search Engine: TraceMoe\nNo results found.`。记录的出站请求为 `https://api.trace.moe/search?cutBorders=true&url=https%3A%2F%2Fexample.com%2Fimage.jpg`。没有替换引擎或伪造 SearchOutcome，只替换了网络响应。上游 `model/tracemoe.py:177-188` 已保存 error 字段，自己适配层没检查它。

应在各引擎适配边界检查其明确错误字段/状态，成功之后才允许 raw=[]；None/缺 raw 应是响应契约错误。对 HTML 引擎的空解析结果不能凭空断言 CAPTCHA，也不能全部改成 error：没有明确证据时保留“不确定/未获取匹配”的提示，不要假装知道“搜索成功”或“机器人验证”。上游网络超时/HTTP 失败目前还会统一落到“内部错误”（`server.py:509-521`）；isError 没错，但可重试的外部故障与自己代码错误混在一起，下一步可加有限、脱敏的分类，别把完整异常字符串放出来。

本轮已实跑完整 check.sh：`PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' bash check.sh`，退出码 0；367 passed in 4.05s，31/31 反向注入被抓，7 步全通过。上述问题不与门禁绿相矛盾，而是门禁没有覆盖的性质。

#### 凭据与参数适配附记

- 已复现配置错误回显代理密码：`credentials.py:328-335` 对缺 hostname 的代理 URL 把 raw 原文拼进 CredentialError，`server.py:500-504` 再返回给工具调用方。仅用合成值 `IMAGE_SEARCH_PROXY=http://user:SYNTHETIC_PROXY_SECRET@`，调用 `_search_image_logic('eA==', engine='Yandex')` 得到 `secret_in_tool_text=True`。这是 cookie 报错脱敏修好、proxy 同类路径漏修；线上变量为空时休眠。应统一脱敏所有代理解析错误，并把非法端口/方括号归成配置错而非内部错误。
- Cookie 限引擎域且 secure=True 方向正确（`credentials.py:49-60,155-195`），但允许该父域所有子域是刻意的宽边界，不是 host-only。移除继承代理的当前实现能起效，构造后删私有属性的维护成本见第 15 问。
- 参数白名单与输入最后落位是有效的双边界（`params.py:308-331`、`server.py:560`）。GoogleLens.q 在 init/search 都声明，但 validator 优先 init 后 continue（`params.py:224-230,320-326`），“search 覆盖 init”从这个平面接口不可达；不是当前搜索坏了，是契约描述给了不存在的选择。`params.py:289` 写 null 拒绝，292-293 实际接受，也需校正文案。

## B. 协议与契约

### 4. MCP 2026-07-28 对齐

不能称为“全协议已对齐”。工具结果那一层基本正确，生产 HTTP 入口仍是 legacy HTTP+SSE：`main.py:192-211` 显式 `transport="sse", path="/sse"`，`main.py:274-286` 只有 stdio 与 --sse，没有现代 Streamable HTTP 的 CLI 入口。SDK 的 LATEST_PROTOCOL_VERSION 新，不意味着旧传输自动变成新规范。

已在线核对官方 2026-07-28 tools、transports/streamable-http、authorization 页面（链接见末尾）：

- isError 区分工具执行失败，成功提供 schema 匹配的 structuredContent：方向正确，完整门禁已通过；上游错误误判为空结果是应用语义漏洞（第 3 问），不是字段拼写问题。
- outputSchema 可选；一旦声明，成功结构化结果必须遵守它。错误出口 ToolError 不应为了凑成功 schema 而伪造 results。
- 名称、title、只读/幂等/openWorld 注解符合这个工具的语义（`server.py:161-171, 218, 629-633`）；查询重复可能耗配额，不等于 idempotentHint 必须 false，该提示描述状态副作用，不承诺结果恒定或免费。不要误以为注解能代替访问控制。
- 明确未做的 SHOULD：结构化内容同时序列化 JSON 放在 TextContent；legacy HTTP+SSE 迁移到 Streamable HTTP；HTTP 授权遵循 OAuth 资源服务器发现流程。静态 bearer 自用可接受，但不是 OAuth 兼容服务器。
- 文本不加 JSON 可保留为有理由的 SHOULD 偏离，但 `server.py:592-594` 的“每个字段都出现了”论证不严谨：机器字段与人类文字并非相同键值序列化，且 null 字段会被文本省略。应诚实记录“保持既有文本、不提供 JSON 文本镜像”，而非声称已满足 SHOULD。
- 规范 tools/security-considerations 的限流是 MUST，不是 SHOULD。本仓调用路径没有工具限流/并发容量控制（`server.py:525-586`）；是否由外部网关补足未验，已知的静态 token 不是限流。入站 JSON/body 大小也没有应用级配额，Base64 解码限制发生在 JSON 已接收之后。
- 默认回环、有效 bearer、Origin 校验在认证模式下已做（`main.py:126-168, 276, 301-308`）。匿名模式跳过整个 AuthMiddleware，连 Origin 检查也跳过；回环不等于不受浏览器 DNS rebinding。匿名开发模式应保留 Origin/Host 防护，而不是把它与 token 开关绑定。本轮对未套中间件的真实 SSE app 发送 Host/Origin=attacker.invalid 的 POST /messages/，得到 400 `session_id is required`，不是 Origin 403；请求已到协议处理层，未实测完整浏览器攻击链。
- 稳定工具列表顺序、生命周期通知、现代 metadata/header-body 一致性、真实 stdio/HTTP wire 的 resultType 与取消行为，本轮未完整验证；不把客户端 SHOULD（UI 确认、校验结果等）算作服务器漏项。X-Accel-Buffering 建议属于新传输；当前 legacy SSE 的实际响应头与远端 nginx 未验。

建议以加法暴露 /mcp 的现代传输，保留旧 /sse 兼容入口；这是迁移理由，不建议为本次审查大改框架。

### 5. outputSchema 的取舍

这个取舍是对的，不是“上游加字段就会破约”。`server.py:195-210` 显式投影四个字段，并不把上游对象整个 dump 出来；上游多出 episode/author_url 不会进入输出，additionalProperties:false 正好约束适配层。它只写在 results 的 item 上（`153`），顶层 `132-158` 没禁止额外属性，任务书若理解为整份响应都封闭，要纠正。

代价是要做按 episode/author 自动处理的 agent 仍得解析文本。只有出现明确的机器消费需求时，才加可选的 engine_details/extensions 或版本化 schema；不应为预想需求塞一个 Any 字典。

真正需要收紧的不是多字段，而是语义：result_count/returned 应非负且与 results 长度一致，truncated 应与二者关系一致；JSON Schema 普通约束不能完全表达跨字段关系，要靠构造函数/测试。similarity 保留 number|string|null 合理，但浮点 NaN/Infinity 在 Python 属于 float，却不是合法 JSON 数字（`server.py:182-192`）；目前没有 finite 检查，需一条明确的序列化反例测试。

### 6. 未知工具的框架偏差

同意不自己包一层去和框架硬扛，当前收益低于适配维护成本。官方 tools/error-handling 明确未知工具应为 JSON-RPC error；`tests/test_mcp_contract.py:274-292` 把框架返回 isError 的事实标记为已知偏差，完整门禁本轮也实际验证了它。

实用影响：客户端用 exception/JSON-RPC code 路由的配置故障监控不会触发，agent 可能把工具拼错当成可重试业务失败；但能处理 isError 的个人 agent 通常仍能从文本纠正，不是无声成功。因此“可接受”有条件：你的客户端确实处理 isError，且升级框架后这条测试需人工重新裁决。未来框架修对了时，不该为了保绿再人为改回错误行为；把它归为版本锚定的特征测试，而非永久业务契约。

## C. 需要直接反驳的判断

### 7. 删除内层期限检查

反驳：论证不成立，已有离线反例。外层 wait_for 依赖事件循环调度；`async for`/`await` 并不保证真正让出事件循环。缓存中连续可读的 chunk、同步解析/拷贝、或异步接口中的同步工作，都会使定时回调迟迟不能运行。内层 monotonic 检查在下一块到达时可以先发现已经超时。

最小实测（完整探针路径见末尾）：自定义 httpx.AsyncByteStream，20 次各同步忙等 6ms，再 yield 1 字节；无真正的 await 挂起点。调用未改动的 SafeDownloader.fetch，预算 20ms，得到：

`busy_chunks budget=0.02 elapsed=0.135 success bytes=20`

这是对“外层永远先到”这个抽象断言的确定反证，不等于已经证明真实公网 httpcore 可以稳定制造同等阻塞。原有滴流测试每块 sleep，恰好主动给了外层取消机会，只证明那一种调度形态。

另外，等待取消清理也可能超预算：stream 读时 sleep，aclose 耗时 60ms，得到 `slow_close budget=0.02 elapsed=0.082 timeout closed=True`。因此文档对“清理也必须在期限内结束”的承诺错误（`safe_download.py:352-370, 547-549`）。

建议：保留外层取消；在共享的同一 deadline 下恢复循环检查，并在返回前检查是否过期（不重新起算预算），增加“不让出循环”和“慢清理”两个独立场景；将保证写成合作式取消的截止期限，不承诺进程级硬实时。阻塞工作要移出事件循环或另设限制。不要为了某条 mutation 能变红而删有不同触发条件的保护；删变异不敏感的重复测试也比删保护更合理。

### 8. 192.88.99.0/24 与 NAT64

当前运行环境实测：192.88.99.2=False，64:ff9b::808:808=False，64:ff9b:1::7f00:1=False，2002:808:808::=True。后两类分别来自 is_reserved/全局性检查和 6to4 内嵌 IPv4 递归（`safe_download.py:139-159`）；NAT64 并没有独立的显式整段拒绝表。

判断：维持这两类保守拒绝，不改成更精细的放行。IANA 官方表实核：192.88.99.0/24 是 deprecated 6to4 relay；192.88.99.2/32 另列为 6a44-relay，Globally Reachable=False。因此“整段都只是已废弃 6to4”这个注释不精确，但整段拒绝对本服务合理，不能把 .2 作为应恢复访问的普通公网反例。

NAT64 的公网翻译目的（如 64:ff9b::808:808）也拒绝，确实比“只拒内嵌私网 IPv4”更宽；对于当前双栈/IPv4 可用的公网取图服务，这是可接受的保守策略。若未来明确支持仅能 NAT64 出站的部署，才引入“可信已配置前缀 + 正确解码内嵌 IPv4 + 两端地址策略”并测试，不能只看最后 32 位普遍放行。

实现上应把承诺拒绝的前缀显式列入策略及完整正反例，避免单靠解释器 is_reserved 漂移；现在显式表只有 fec0::/10 和 192.88.99.0/24（`119-124`）。不能把“WKP NAT64 被拒”写成“所有 NAT64 被识别”，网络特定翻译前缀例子和前提见第 1 问。

### 9. 部署脚本默认不覆盖

安全默认是对的，不能为“一键”牺牲已有身份凭据。更好的交互是显式分“仅升级包”“更新配置”“替换单元”，先展示将改的路径/服务名/版本，不展示 secret；备份与功能校验不能省。

但当前实现没有兑现干净的边界：

- `image_search_mcp_deploy.sh:105-118` 在检测“保留旧单元”之前就重写 .env；旧单元若本来引用该 .env，照样改到了运行凭据。只保留 unit 文件并不等于保留运行配置。检测保留/替换应在任何配置写入和安装之前。
- `53-81` 的默认更新分支会提前 exit，--replace-unit 只被解析、不影响这个分支；已有 .env 时默认一路回车并不会替换，须先回答 n，帮助文本没解释。
- `99-114` 仍提示“通用 Cookies”，写无归属的 IMAGE_SEARCH_COOKIES，既不问所属引擎也不保存 *_ENGINE；与新 credentials 契约直接冲突。已有 scoped cookies 也会在重写时被丢弃。
- `77, 200-201` 把 is-active 放在 echo 的命令替换里，非零状态被 echo 的成功掩盖，服务失败仍能打印“完成”；本轮只把 systemctl 桩改为 `is-active` 输出 inactive、exit 3，未改部署脚本，得到 `exit=0 reports_update_complete=True reports_inactive=True`；需要显式检查退出码和 /healthz（再加一次认证工具冒烟），不能只显示状态。
- 升级动作是 uv tool upgrade/install（`60`），但线上 ExecStart 是另一目录 start.sh 内的 uvx。两者是否同一个用户、解释器/工具环境、版本，本轮未在远端验证；不能从“重启单元名正确”推导“运行了刚升级的版本”。

保留“不覆盖”决策；修的是配置/升级/验证语义，不是删掉安全开关。

### 10. 两个脚本门禁

思路都可保留，不值得引入专门的 shell 测试框架，但现在有重要瑕疵。

1. step 函数抽取：`tools/gate_check_sh_exit_code.py:65-79` 用起始标记和第一个独占行 } 截取；对当前简单函数足够，未来有嵌套独占 } 会截错，且子进程没 timeout、没复现 check.sh 的 set -uo pipefail。更重要的是 `114-115` 用 stdout 含“假的失败步骤”来证明 FAILED_STEPS 更新——函数开头就会打印名字，删掉数组追加仍能过。这是确实的弱断言。应打印/断言数组内容、子进程退出码，给 timeout，并加一次完整脚本失败汇总测试。
2. 部署桩测：`tools/gate_deploy_script_unit.py:74-94` 固定已有 .env、回答 y，所以只测更新分支，没测保留/覆盖/备份，也没测 inactive 情况。`105` 的 substring 会接受 `image-search-mcp-wrong`，需按记录的 argv 精确比较。`env` 继承宿主 IMAGE_SEARCH_UNIT，而默认场景没有清掉它（`77-84`），有环境污染。
3. 最严重的维护风险：`gate_deploy_script_unit.py:150-161` 反向自检直接改写仓库的部署脚本，再 finally 还原；并不是在临时副本注入。外层超时/SIGKILL 或并发运行可留下损坏源码。本轮 check.sh 运行后 `git diff --exit-code -- image_search_mcp_deploy.sh check.sh src tests tools` 为 0，说明这次已还原；但用户“整份 check.sh 不动工作区”的前提不成立。之后不再在原工作区重跑这个自检。
4. SUDO_STUB 是 exec "$@"（`38-40`），不是拒绝真实系统操作的沙箱；当前 y 分支只撞到伪 systemctl，未来测试覆盖到创建分支，sudo bash -c 会真的写 /etc。应把脚本先复制进临时目录、改可注入 UNIT_DIR；桩只允许已知动作，未知调用非零；记录 argv 到文件并检查精确调用序列，失败/备份/保留分支都加入。

同样便宜的替代：让 run_deploy(script_path, unit_dir, env) 接收副本路径；在副本做反向变异，PATH 桩只负责隔离外部副作用。并非“复制一份逻辑”，而是运行同一份源文件的临时副本，与已有 reverse_tests 做法一致。

## D. 其他维护问题

### 11. 打包元数据

依赖策略的论证是错的（`pyproject.toml:26-34`，README:267-274）：上界不能防住所有变化，不等于上界没有价值；契约 CI 与锁定发布是不同机制。公开范围 `PicImageSearch>=3.12` 会接纳未来破坏性发行版，生产 uvx 解析/升级不会先跑本仓测试。坚持不跟 main 是合理的，但 main 一旦发成 PyPI 新版本，无上界会自动越过这个选择。

建议用“经测试的兼容版本范围 + 固定发布依赖解析结果”，生产安装精确包版本/受约束制品；另外定期在最新允许依赖上跑 CI 作为升级预警。不要把测试时的 3.12.11 等同于部署时必然的 3.12.11。直接导入的 httpx、mcp、starlette 也应显式声明相容依赖（`safe_download.py:48`、`server.py:39`、`main.py:216-217`），不能一边强调 jsonschema 不依赖传递安装，一边漏掉运行时直接依赖。

- Python：本轮实际解释器 3.11.16；已安装 fastmcp 4.0.10、mcp 2.3.0 均声明 >=3.10，PicImageSearch 3.12.11 声明 >=3.9，httpx 0.28.1/httpcore 1.0.9 声明 >=3.8。没有从这些元数据发现项目 >=3.10 的硬冲突。3.10/3.12 只看到 CI 配置，没实际跑过；还存在第 12 问 CI 启动错误。声明最低版本不等于承诺已测未来每个 Python 版本，不建议仅因本地只有 3.11 就把下界武断升高。
- authors：`pyproject.toml:12-14` 已从基线 Your Name 占位改为 Thetail，仓库作者元数据不再是假占位；本轮不做外部身份/版权权属证明。
- license：`pyproject.toml:11,19` 与 LICENSE:1-21 的 MIT 一致；LICENSE:25-31 有 PicImageSearch 归属说明。作为调用依赖的包装层，方向合理；本轮未审核所有依赖/历史代码的版权链，也未构建 wheel 验证许可文件是否实际进入制品。后续可用现代 SPDX license 字段和明确 license-files；目前老表格写法本身不是运行 bug。
- 版本仍为 0.2.1（`pyproject.toml:7`）。未发布开发分支保持版本可以，但发布新的安全契约应有明确新版本/发行说明与可回滚制品，不能用启动日志的旧版本号冒充已部署验证。
- 可选能力：load_proxy 接受 socks5/socks5h（`credentials.py:329`），本轮 `socksio_installed False`，真实 Network(proxies='socks5://127.0.0.1:1080') 构造直接 ImportError。应增加有说明的 socks extra 或在配置校验时给可操作的缺依赖错误，不能只列“允许 socks5”而装完默认包就不可用。

### 12. 测试的诚实性

不是“这套测试不可信”：文件覆盖回归、错误脱敏、输出 schema 正反例、整次反向门禁都有实际价值。但还有下面这些证明缺口；367 绿不能替它们作证。

1. **已复现，重复头假绿。** `main.py:106` 先 dict(scope.headers)，重复头最后一个获胜；`tests/test_auth_asgi.py:242-254` 只测“有效在前、无效在后”，恰好符合 last-wins，也就过了。直接向 ASGI 传原始头列表，合成 token，输出：valid/invalid→401；invalid/valid→204；valid/valid→204。它并没有实现声称的“重复必须拒绝”。不能把此问题说成无 token 即可绕过：成功分支仍需要有效 token；实际缺陷是代理解析歧义与安全契约未实现。修法：dict 之前数 Authorization/Origin 次数，重复即拒绝；两个顺序、两个相同值都做断言。
2. **已复现，step 门禁漏验数组。** 只在内存里去掉 `FAILED_STEPS+=(...)`，用原 gate.check 检查，返回 `[]`（认为没有问题）。证据 `gate_check_sh_exit_code.py:114-115`；名字在 echo 里出现不等于追加进数组。要断言数组内容与完整脚本最终 exit，见第 10 问。
3. **已复现，DNS 开口夹具没有恢复原函数。** `conftest.py:88-113` autouse 先把 socket.getaddrinfo 换成 _blocked；allow_real_dns 再把这个 _blocked 当 real 存下来。直接按夹具顺序调用，`allow_real_dns_restored_original False`。应与 `_real_async_init` 一样在模块加载时保存原函数，并对“夹具真恢复原函数”做正对照。当前没有依赖它的真实 DNS 成功证据，不能拿这件事反推现有测试全部失效。
4. **DNS pin/SNI 的观测层比说明浅。** `test_safe_download.py:43-58` 的自定义 AsyncBaseTransport 直接返回 Response；`185-200` 看到的是 HTTPX Request，不是 socket connect_tcp / start_tls。能证明传入固定 IP 与 SNI extension，不能证明 TLS 证书按域名校验、连接池或网络后端无二次 DNS。本轮查到 httpcore 1.0.9 `_async/connection.py:105-153` 确实消费 sni_hostname，方向有源码支持。更强且便宜的门禁是在真实 AsyncHTTPTransport/httpcore 下替换 NetworkBackend，记录 connect_tcp/start_tls；至少一次本地 TLS 成功证书/错域证书反例。
5. **期限测试只有会让出事件循环的滴流。** 见第 7 问忙等反例。测试名称可保留，不能把“定时 sleep 的流被停掉”扩大为任何执行路径都硬截止；也没有连接 aclose/cancel 的直接断言。增加受取消流/关闭计数器，断言 CancelledError 向上传播且关闭一次，另测慢关闭。
6. **已复现，CI 形态会坏，本地绿遮住了它。** `check.sh:36-39` 无 .venv 时 PY=python3，`128` 却把它传成 `$(pwd)/python3`。`.github/workflows/ci.yml:54-68` 的 setup-python + pip -e 并不创建项目 .venv。临时副本按此布局执行 `bash check.sh --reverse-only`，最终 `FileNotFoundError: .../python3`、退出码 1。修法：用所选解释器打印 sys.executable 或 command -v 转绝对路径，不能拼 cwd；把“有 venv/仅 PATH”两种启动方式列为门禁。第一次自建副本漏复制 LICENSE 导致先报 LICENSE 缺失，补全 COPY_ENTRIES 后才得到这里的真实反例，未把探针自身错误冒充产品错误。
7. **反向测试比它的宣称弱。** `tools/reverse_tests/run.py:147-169` 检查 returncode==1、测试名与 FAILED 文本；pytest 的 TypeError/RuntimeError/夹具失败也可给 1，不能证明“干净断言失败”。超时某分支甚至明确算 caught=True（`157-164`）。应读取 pytest 的结构化报告/JUnit，校验指定用例失败阶段和异常类别；非预期错误归门禁自身失败。来源校验 `111-117` 又是一个空循环问题：stdout 为空时直接通过，没有先断言恰好四个模块路径。加输出数量/绝对路径核验和空输出反向自检。
8. **环境和契约覆盖范围不完整。** `conftest.py:24-35,60-62` 不清空带引擎后缀的 cookies、COOKIES_ENGINE、ALLOW_ANONYMOUS/ALLOWED_ORIGINS、ALL_PROXY；应按配置前缀清理并放行测试自设值。`test_param_contract.py:69-94` 验签名名字，不验默认值或参数真的改变出站请求；原本无参数的引擎空循环是合理的，但不要称为“语义全覆盖”。`test_readme_examples.py:44-61` 的全局非空保护做对了，不过漏了 engine/source 的坏示例会被抽取器跳过，只剩其他好示例时仍绿，应检查所有标记为工具调用的示例或用明确示例标签。

本轮没有穷举每个测试分支；以上是定向复核中有源码或探针支撑的具体反例，而非覆盖率猜测。

### 13. README 一致性

参数名主体已修对：README:173-189 的 cut_borders/bovw 与白名单一致；本轮原测试也验证过。仍有明确冲突：

- README:106 声称引擎请求仍读 HTTP_PROXY/HTTPS_PROXY，实际 `server.py:564-575` 拆掉继承代理，且 `docs/配置变更说明.md:91-98` 已正确说明新行为。入口文档直接会把用户指错路，优先改。
- README:108-111 说无后缀 IMAGE_SEARCH_COOKIES 一概不接受，实际 `credentials.py:281-300` 允许搭配 IMAGE_SEARCH_COOKIES_ENGINE，且 scoped 变量优先。应说“无归属的全局值拒绝”，别说变量已完全废弃。对应 README 测试也写成了过强限制（`test_readme_examples.py:105-120`）。
- README:11 把“空结果提示 cookie”描述成识别了 Bot Protection；实现 `server.py:599-605` 并没检测 CAPTCHA，只按引擎名和 raw 空判断。README:114 的“没结果就配 cookie”对 Bing 缺签名解密尤具误导（`params.py:236-240` 已知原因不同），还可能是本来没匹配或限流。改成排查顺序：看明确失败信息/引擎状态，有验证证据才试 cookie，不承诺 cookie 能绕过风控。
- README:46/56/134 的 `your_cookies` 看似占位，原样照抄是非法 cookie；换 `sid=replace_me` 并注明真实值从自己的会话获取。部署脚本又在收全局 cookies，见第 9 问，是同一个配置契约仍有两份副本。
- README:32 单独的远程启动命令在未先设 token 时失败，后文有说明但快速复制易踩；把前提放在命令旁。ASCII2D（第 9 行）与实际 Ascii2D 不同，正文参数表用了正确拼写，展示名不应被误当调用标识。
- README:263-274 的“main 领先 78 提交”和依赖版本叙述是某次快照，不应当长期依赖策略；不加上界理由错误，见第 11 问。README:258 的“CI 覆盖”仅表示配置存在，还没有这批未推送提交在 3.10/3.12 真绿的证据。

`docs/实施进度.md:202-206` 的“IMAGE_SEARCH_PROXY 一旦启用，取图侧拒绝组合”也不准确：`server.py:451` 只构造 SafeDownloader()，没有给它传代理；本地取图继续直连，代理只给引擎网络。下载器只有自己接收到非空 proxy 才拒绝（`safe_download.py:342-349`）。直连图源 + 代理引擎的拆分安全且实用，但应把真实行为讲清。

### 14. 半年后最容易腐烂的三处

1. **依赖与凭据适配边界。** 无锁的 PicImageSearch/FastMCP/httpx 更新，加上私有 `_mounts` 手术、静态 COOKIE_DOMAINS、按源码字串识别本地下载引擎（`credentials.py:49-60,198-235`、`safe_download.py:585-588`），会在上游迁移时一起变。固定发布解析结果，增一条真实传输/公共客户端 smoke，升级单独经门禁，比增加更多源代码字符串匹配更耐用。
2. **部署实际生效链与 CI 启动方式。** .env、unit 内嵌凭据、uv tool 与 uvx、两个解释器入口相互独立；当前 gate 只证明 restart 名字（`image_search_mcp_deploy.sh:53-81,105-181`、`gate_deploy_script_unit.py:74-94`、`check.sh:128`）。让版本、进程启动路径、健康与认证冒烟成为成功条件；测试副本隔离，增加无 .venv 场景。
3. **行为说明与变异测试耦合。** README 的环境代理说法已与实现相反；“同一 deadline 永远遮挡”和“raw 空必成功”是写进注释的错误定理（`README.md:106`、`safe_download.py:558-562`、`server.py:606-607`）。源码片段一改，反向注入又要同步改。保留以不变量为中心的测试，记录适用前提，文档集中到单一契约源；变异应服务于性质证明，不应倒过来支配安全设计。

## E. 抽象层判断

### 15. 最没把握的抽象是否过度设计

我最不信的是“先用 PicImageSearch.Network 构造默认客户端，再拆 `_mounts`、补装 cookie jar”的客户端修补层。就当前版本，我判它是局部过度设计：为了继续使用一个已经拿不到所需安全配置入口的薄包装，增加了私有属性手术与对应的复杂夹具。不是整个 credentials.py 都过度设计，也不是 SearchOutcome 过度设计。

源码证据很直接：PicImageSearch `network.py:80-86` 的 Network.__aenter__ 本来就交出 httpx.AsyncClient；ClientManager `132-148` 明确支持直接传 AsyncClient，而且给了 client 就不再用它的其他网络配置。自己的 `_run_search` 也只把 net 传给 Engine（`server.py:564-584`）。因此更简单的边界是由适配层用 httpx 公共构造参数显式给出 trust_env=False、proxy、限定 cookies、headers、timeout、SSLContext，直接传进去；保留凭据解析和域策略，去掉“先继承再拆掉”的过程。修改前要以当前 Network 的 UA、TLS 设置、重定向行为为回归基线，不是凭感觉换一行代码。

我会反转这个判断的具体条件：上游某个已接引擎被证实依赖 Network 独有生命周期/状态，直接 AsyncClient 的真实离线响应测试会失败；或上游 Network 新增公开的 trust_env/cookie-jar/transport 参数，能够无私有手术地表达全部策略。届时优先用它，删除本地重复构造。仅仅“上游可能改”不是继续保留私有 `_mounts` 的理由。

SafeDownloader 的 IP 固定与逐跳策略不算过度设计：普通 follow_redirects=True 的 get 无法满足这条地址边界；params 的白名单/生成说明也在解决实际多引擎契约。别为了删行数撤掉真正有独立职责的层。

## 本轮实测记录

所有 HTTP 引擎响应/危险目标探针均为本地受控 transport，没有扫描公网、访问线上 VPS 或读取真实凭据。官方规范与 IANA 查询是只读公网检索。

1. 仓库快照：初始 `git status --short` 空；`git branch --show-current` 为 `audit/security-and-contracts`；HEAD `36f37bd`；`git rev-list --count 09dda2e..HEAD` 为 9。
2. 本地完整门禁：在仓库根目录执行 `PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' bash check.sh`，退出码 0，`367 passed in 4.05s`、`合计：抓住 31/31`、`通过 7 步`。这是既有测试的实际结果，不表示新发现也被它覆盖。因为发现该命令内一个 gate 修改原文件，不建议再当成严格只读命令运行；要跑请先做整个临时副本。
3. 地址/期限/配置探针：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python /root/.hermes/cache/scratch/image-search-full-review-probe.py`，退出码 0。关键输出已逐项列在第 1、7 问与凭据附记。
4. 门禁/认证/部署/CI 布局探针：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python /root/.hermes/cache/scratch/image-search-full-review-probe2.py`，探针进程退出码 0；它测出的 CI 副本命令退出码为 1。输出包括 `step_gate_without_failure_append []`、`allow_real_dns_restored_original False`、重复头 401/204/204、inactive 服务仍 exit 0，以及无 .venv 的 `.../python3` FileNotFoundError。
5. 单独 MCP 链路探针：真实 TraceMoe 引擎 + mock HTTP error 字段，返回 `trace_error_mcp is_error=False`；本轮未用真实站点执行搜图。真实 SSE app 路由读取为 `[('/sse', ['GET', 'HEAD']), ('/messages', [])]`；匿名恶意 Origin POST 进入 session_id 检查，返回 400。
6. 构造/元数据探针：`https://faß.de/a` 得到 `fass.de` 对 `xn--fa-hia.de` 的主机差异；SOCKS URL 配置校验接受，但 Network 构造因缺 socksio 抛 ImportError。实际安装版本见第 11 问。

两个探针文件是临时材料，可能随运行环境缓存清理；唯一仓库交付物是本报告。关键输入/机制/输出已写入正文，不用依赖临时文件才理解结论。

### 期限反例的最小可重建核心

用以下流作为 `httpx.Response(200, stream=BusyStream())`，由 MockTransport 返回给 SafeDownloader；resolver 固定返回 `['1.1.1.1']`，`DownloadPolicy(total_timeout=0.02)`，调用 fetch('http://images.example/a')：

```python
class BusyStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        for _ in range(20):
            until = time.monotonic() + 0.006
            while time.monotonic() < until:
                pass
            yield b'x'
```

代码在库的异步流接口上合法，大小总共只有 20 字节；它用于否定“wait_for 永远先触发”的定理，不是声称公网 HTTP body 会执行这段 Python。慢关闭反例则让 __aiter__ 先 `await asyncio.sleep(0.2)`，aclose 里 `await asyncio.sleep(0.06)` 并记 closed=True；同样 20ms 预算最终花约 82ms 才报告 timeout。

### 外部规范来源

- MCP 2026-07-28 Tools，结构化内容、错误分类、Security Considerations： https://modelcontextprotocol.io/specification/2026-07-28/server/tools
- MCP 2026-07-28 传输概述： https://modelcontextprotocol.io/specification/2026-07-28/basic/transports
- Streamable HTTP，包括 legacy SSE 迁移 SHOULD、Origin、取消与 metadata： https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
- Authorization 的适用条件与 HTTP SHOULD： https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- IANA IPv4 Special-Purpose Address Registry（192.88.99.0/24 与 .2/32）： https://www.iana.org/assignments/iana-ipv4-special-registry/iana-ipv4-special-registry.xhtml

## 发布判断与建议优先级

**维护者结论：核心修复路线正确，但实现完成度被门禁与注释高估；不建议按当前 HEAD 直接上生产。** 没发现理由推翻输入白名单、固定 IP 的逐跳下载器、独立 SearchOutcome、四字段投影、保守特殊地址策略或已有单元默认保留。不上游 main、不自己维护 Bing 解密、暂时记录框架未知工具偏差，也都可以保留。

发布前应先收口以下具体项，而不是继续漫无目的扩审：

- **P1，正确性/发布可信度：** 明确失败的 TraceMoe 响应不能被报成成功空结果；修复无 .venv 的 CI 解释器路径；反向自检全部迁到副本；部署失败必须非零，配置输出不能继续违反新的 cookies 契约。对应第 3、9、10、12 问。
- **P1，凭据保密（当前休眠）：** 错误代理 URL 不得回显 secret。虽然线上 proxy 为空，不代表发布包应保留这类漏修。对应凭据附记。
- **P2，地址/异步边界与契约：** 统一 IDNA 主机规范化；修正“唯一 wait_for 硬期限”论证并补独立的读取检查/测试；给整个搜索过程设置总预算和容量约束；修重复认证头、匿名 Origin 开关耦合；把下载字节上限与进程内存上限明确区分。这里有已复现的确定问题，也有明确标注的环境相关风险，不同等当成可远程利用漏洞。
- **发布流程要求：** 发布依赖需要可重复，README 的代理指引要与实现一致；冻结版本/制品后再做真实 stdio、legacy SSE 客户端及最小部署冒烟。只读查询 /healthz 不能替代认证成功调用与回滚验证。源码 API 面检查不等于 wheel 或远端实际版本检查。
- **可以接受的已知偏离/非阻断项：** 不额外塞 JSON 文本镜像；不包装未知工具 JSON-RPC 错误；暂保留 legacy SSE；不为 NAT64 公网翻译特例现在引入复杂解码；引擎特有字段先留在人读文本里。前提是文档不称为全面规范对齐，也不隐瞒消费者限制。

未验证边界：真实远端部署、TLS/SNI 的 socket 级端到端证书反例、3.10/3.12 实际 CI 结果、wheel/sdist 产物、真实搜索引擎有效率、真实代理与 cookies、框架双重取消/服务退出时的连接清理，以及部署环境自定义路由/NAT64。它们不是本轮新造的“漏做任务”，不把未验证写成已失败，更不把已知的旧生产代码当作当前分支行为。

复核范围是一次全仓定向质量裁决，而非逐行安全证明。已优先把高不确定模块与可复现反例落盘，再完成其余问题；没有因为数量不足继续制造问题，也没有为了门禁全绿回避反驳已有判断。
