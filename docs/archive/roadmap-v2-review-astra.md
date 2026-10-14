结论：主要整改方向正确，撤掉全面 compat 层和不承诺自修 Bing 都成立。但 v2 还不是“拿到就能完整开工”的任务包：1-4／1-6 的安全网络契约没有定下来，首批验收还有假阳性，CI 也仍没有实施条目。

本轮未修改项目文件，未创建探针文件，未安装依赖，未访问生产服务。使用已有指定版本环境，探针以内存执行，HTTP 使用 MockTransport。要求阅读的九份文件，执行前后哈希一致。

下文 server.py、main.py 均指 src/image_search_mcp/ 下的文件；PicImageSearch/ 开头的路径指已安装的 3.12.11 源码。

Q1. 整改对不对？

一、撤掉 compat.py：对，但必须准确理解“撤掉的是什么”

你撤掉的是“为了集中 import、按版本号分支而建的全面兼容层”。这个决定正确。

上一轮意见不是“不需要适配”，而是：

    现有服务已经在适配 PicImageSearch。
    再搬一遍代码，不会自动获得契约保障。

现在采用“真实 Network／引擎类＋MockTransport＋具体请求和结果断言”，能直接覆盖当初出错的边界。例如：

- Network 参数从字符串被误改为 dict，真实构造会失败。
- 参数名被上游改动，测试会在真实调用或实际请求字段上发现。
- 上游仍接受 **kwargs、但悄悄忽略参数，具体请求字段断言会发现。
- 模型字段变化，真实解析器和格式化断言会发现。

但“上游改了签名没人知道”不是仅靠这些测试自动解决的，还需要三项运行机制：

1. 锁定生产依赖组合，避免重建时未经审查升级。
2. 对候选依赖组合实际运行同一套测试，失败阻止合入或发布。
3. 对远端网页变化另做低频在线抽验；离线 fixture 无法发现网站今天改版。

你的第二章第 4 条和 2-4 已写出方向，理解正确。不过“谁自动运行、什么时候运行、失败在哪里报告”还没落实，见 Q6。

也不要把这次决定扩张成“不能新增任何网络适配代码”。1-4／1-6 完全可能需要一个很薄的 client 工厂、安全下载模块或出站策略边界。这与恢复全面 compat.py 是不同的事。

二、不承诺自修 Bing：对，不是在给“不做”找理由

docs/ROADMAP.md:172 的判断成立。

把签名解密、请求重建、解析样本、异常处理放进适配层，维护工作不会消失。文件名不能替你承担上游页面变化。

你还保留了“小范围回移植／受测 fork／等待正式版本”的后续选择，没有把“不承诺本轮完成”写成“永远不修”，这点也对。

需要维持两个限定：

- “容易变成半个上游分叉”是维护风险，不是任何单个补丁都会必然造成的结果。
- “不支持”和“实验性”最终要选清楚。前者应拒绝调用并解释原因；后者需要说明不保证成功。不要同时模糊承诺。

当前 Yandex 优先、没有明确 Bing 必需需求的前提下，停止承诺 Bing 修复，是范围管理，不是逃避。

三、附录 A.1 逐条判断

1. “撤回 TraceMoe 标题永远为空”：部分对。

撤回原结论完全正确，完整调用确实会补标题。

但你紧接着换上的“title_chinese 已取到但不展示”仍然说过头了。能确认的是：

    如果对象中有非空 title_chinese，当前格式化器不展示。

不能仅凭 update_anime_info 中存在赋值语句，就确认中文标题实际上已经取得。详细证据见 Q2。

2. “三个白捡引擎不是一行映射”：对。

Lenso 的弃用警告成立；AnimeTrace 的现有格式化输出确实为空。删除 Lenso 接入承诺、把 AnimeTrace 拆成参数／格式化／错误处理／验证，是正确整改。

补充一点：Lenso 当前根本不在 ENGINES 中，不需要为了“摘牌”先接入它。3-3 可以描述支持状态，3-4 不接入即可。

3. “.gitignore 不是全局测试阻塞”：对。

只针对特定文件名的忽略规则，不等于整个 tests/ 被忽略。将它并入首批测试工作是正确的。

但 1-8 的新验收命令本身不正确，见 Q3。

4. “不安装 main 不等于不能取任何 commit”：对。

整套安装未发布上游，与审查后回移植一个补丁，不是同一个风险范围。你没有改歪。

5. “共享 client 不是跨域 cookie 的根因”：对。

无域 cookie 才是当前直接原因；重复创建带相同无域 cookie 的 client，仍然泄露。

“新建 client 不提供隔离”应理解为“仅靠新建这个动作不提供所需隔离”，不能扩大成“独立 client 永远没有隔离价值”。独立无凭据下载 client 仍然是很有用的边界。

6. “Yandex 兜底不依赖 compat”：对。

安全下载、错误识别和总期限才是真前置。不过现在“错误识别”还没具体到足以决定是否兜底，见 Q2、Q4。

7. “健康端点和在线搜图拆开”：对。

正确，而且不是过度设计。轻量存活检查、本地就绪、外部引擎抽验的失败后果不同，不应混用。

8. “撤回靠猜、升级必坏”：对。

新表述转向受测依赖、发布验证、部署复现，符合证据边界。

四、附录 A.2 逐条判断

1. 条件性认证绕过前移：对。

1-2／1-3 不依赖结果结构化、全面兼容层或引擎扩展。前移合理。

2. 首批自带最小测试设施：部分对。

1-7 补上 conftest、MockTransport 和 check.sh，解决了“先修完、以后再有测试”的矛盾。

但上一轮明确要求的最小 CI 接线没有写进去。现在仍可能变成“有一个手动脚本，但发布时没人运行”。

3. 首批修复范围和验收已消歧：部分对。

比 v1 明确，但还没有完全消歧：

- docs/ROADMAP.md:124 的最终验收只点名 1-1 至 1-6，没有纳入 1-9。
- 同处写“本批只修①～④”，但本批还包含缺 token 开放、SSRF、Origin 和 WebSocket 加固。
- “正反测试全绿”应区分：正常代码跑测试通过；故意恢复缺陷时，相应测试必须失败。不是破坏后的测试也应该绿。

五、附录 A.3 的保留判断

逐条结论如下：

- episode=0／To=0 丢失：对。
- SauceNAO similarity 丢失：对；但 URL 丢失不对，见 Q2。
- limit=-1／limit=0 的描述：对。
- 畸形 Authorization 导致 500：对。
- 没有 Origin 拒绝：对；“实测 200”需要标明到底测了哪个入口，真实 /messages/ 在我的探针中是 400，但已经进入协议处理。
- cutBorders 参数无效：对。
- WebSocket 越过认证分支，但不能据此声称匿名调用工具：对。

本轮对应原始输出：

    TRACE_ZERO 'Time: 0.0s - ?s\nFilename: synthetic-episode.mkv'

    LIMIT -1 'Found 3 results (showing top -1):' formatted_items= 2 requests= 1
    LIMIT 0 'Found 3 results (showing top 0):' formatted_items= 0 requests= 1
    LIMIT 1 'Found 3 results (showing top 1):' formatted_items= 1 requests= 1

真实 main() 装配的 ASGI 应用输出：

    anonymous_root 405 'Method Not Allowed'
    anonymous_messages 401 'Unauthorized: Invalid or missing Bearer token'
    Received request without session_id
    valid_token_bad_origin 400 'session_id is required'
    Received request without session_id
    valid_token_no_origin 400 'session_id is required'
    malformed_authorization 500 ''
    WS_MESSAGES KeyError 'method' accepted= False

这里的恶意 Origin 是 https://evil.invalid。400 来自“缺少 session_id”，不是 Origin 防护，因此不能把这个 400 算成安全拒绝。


Q2. v2 引入了新错误吗？

有。最明确的是两个结果映射判断；另外有几处实现契约或验收逻辑没有闭合。

一、SauceNAO“已拼好的 url 丢失”：错

位置：

- docs/ROADMAP.md:66
- docs/ROADMAP.md:136

server.py:171–172 已在引擎专用分支之前输出所有非空 item.url。SauceNAOItem 的 URL 并没有被跳过。

用真实 SauceNAOItem，输入合成 Pixiv 标识后的原始输出：

    url https://www.pixiv.net/artworks/12345 author_url https://www.pixiv.net/users/67890 similarity 95.21
    SAUCE_OUTPUT_BEGIN
    Title: SYNTHETIC_TITLE
    URL: https://www.pixiv.net/artworks/12345
    Thumbnail: https://synthetic.invalid/thumb
    Author: SYNTHETIC_AUTHOR
    External URLs: ['https://synthetic.invalid/original']
    SAUCE_OUTPUT_END

正确任务应保留 similarity、author_url；撤回“补回已经存在的 URL”。

否则实现者可能重复输出 URL，或者为修一个不存在的缺陷改坏通用格式化分支。

二、“TraceMoe 中文标题已取到”：部分对，证据仍然越界

位置：

- docs/ROADMAP.md:67
- docs/ROADMAP.md:233

上游 PicImageSearch/engines/tracemoe.py:13–42 的 GraphQL query 只请求 native、romaji、english，没有请求 chinese。

:154–155 是：

    如果允许中文标题，就从 title.get("chinese", "") 取值。

这只能证明它“能够接收这个字段”，不能证明实际响应包含该字段。

我分别跑了两组完整链路：

- 返回 query 中请求的三个标题字段。
- 在合成响应里额外注入 chinese。

原始输出：

    ANILIST_REQUEST id=123 query_contains_chinese=False
    FINAL_TITLES ('SYNTHETIC_ENGLISH', '')

    ANILIST_REQUEST id=123 query_contains_chinese=False
    SYNTHETIC_CHINESE_INJECTED titles= ('SYNTHETIC_ENGLISH', 'SYNTHETIC_CHINESE') shown_in_output= False

所以：

- “存在非空中文标题时，当前格式化器不展示”：成立。
- “真实服务已经取得中文标题”：本轮没有证实，你这份计划提供的依据也不足。
- “只加一行展示，用户就会看到中文标题”：不能承诺。

合成响应中额外塞一个字段，只能验证处理能力，不能证明真实上游会提供它。这里不能再重复“拿中间状态推断完整行为”的旧错误。

三、1-4 同时写“传字符串”和“换成 CookieJar”，缺少真正的接入方式

docs/ROADMAP.md:117 的目标正确，但字面实现存在冲突。

Network.__init__ 在 PicImageSearch/network.py:51–54 对 cookies 调用 split。它不能直接接收 httpx.Cookies：

    Network(cookies=CookieJar) AttributeError 'Cookies' object has no attribute 'split'

这不说明 CookieJar 方案错了，而是任务缺了“在哪一层装入 jar”。

一个已经实测可行的窄方案是：真实 Network 不接收无域 cookie 字符串，在任何请求之前，将限域 jar 装到它提供的真实 AsyncClient 上。原始输出：

    SCOPED_NETWORK GET https://yandex.com/api Cookie=sid=SYNTHETIC_SCOPED
    SCOPED_NETWORK GET https://untrusted.invalid/image Cookie=None
    SCOPED_NETWORK GET https://yandex.com/user-image Cookie=sid=SYNTHETIC_SCOPED
    SCOPED_NETWORK_CLOSED True

最后一个请求还说明：只限域，不能满足“用户图源下载不带引擎凭据”。用户图源如果恰好与引擎同域，带凭据的 client 仍会发送 cookie。

因此 1-4 必须同时定义：

- 凭据属于哪个引擎。
- 凭据允许发往哪些域。
- 用户图源使用哪一个无引擎凭据的下载 client。

三者不能互相替代。

四、错误分类前移正确，但“能区分五类”的实现难度被隐藏了

docs/ROADMAP.md:132 仍然更像目标，不完全是已明确的实现任务。

例如 PicImageSearch/model/yandex.py:108–115，将 sites=[] 和缺少结果字段都处理为 ParsingError。仅在 _search_image_logic 外层捕获异常、按异常名字或文本分类，不一定拿得到足够信息。

我对真实 Yandex.search 分别提供“合成空列表”和“合成缺字段”页面，原始输出相同：

    synthetic_empty_sites ParsingError [yandex] Failed to extract search results from 'data-state'
     Details: This usually indicates a change in the page structure or an unexpected response.
    synthetic_missing_sites ParsingError [yandex] Failed to extract search results from 'data-state'
     Details: This usually indicates a change in the page structure or an unexpected response.

探针最初的 HTML 没有完整外层文档，两个样本都停在更早的 DOM 查找失败：

    synthetic_empty_sites ParsingError [yandex] Failed to find critical DOM attribute 'data-state'
    synthetic_missing_sites ParsingError [yandex] Failed to find critical DOM attribute 'data-state'

我补全内存 fixture 的 html/body 后，才得到前面那组有效结果；没有把第一组失败当作分类证据。

这项需要补两条规则：

- 哪些状态从 HTTP 层、原始响应或引擎特定字段提取；信息不足时如何保守归类。
- no_results 是正常搜索结果状态，不能因为任务叫“错误分类”，就一律映射为 MCP isError=true。

此外，五类状态里没有“源图抓取失败”的可识别原因。3-2 不能仅看到 upstream_error 就自动改上传，否则 CAPTCHA、解析器失效、限流都可能多打一次请求。

可以保留五类对外状态，但内部至少要有足够明确的原因信息供兜底决策使用。

五、4-1 的健康检查拆分：对；还需要规定谁使用哪一种状态

这里没有方向性错误，但还缺消费规则：

- /healthz：轻量存活检查，不访问引擎。
- 本地就绪：本地配置、应用装配、必要初始化是否完成。
- 引擎抽验：记录最近尝试、最近成功、失败类别与数据新鲜度。

外部抽验失败不能让存活探针失败；否则即使拆出了三个字段，运维仍可能拿“综合红灯”重启进程，重启循环问题照旧。

本地就绪也不应因 Yandex 临时 CAPTCHA 就反复切为未就绪。是否降级对外服务，要另定策略。

六、第二章设计原则没有原则性错误，但不能当实施条目使用

“候选升级验证”“默认安全”“低频抽验”“回归测试”写在原则里，不等于已经接上执行流程。尤其 CI，确实仍然缺实现落点。


Q3. 批次 1 的九条，作为工程任务描述够格吗？

整体判断：有的可以立即开始，有的只有正确目标。不能把整张表视为同等成熟。

1-1：部分够格；完整白名单还不能不问就写

改什么、为什么，清楚。验收还缺两块：

第一，实际允许的参数契约。

不能直接把当前 ENGINE_INFO 当白名单。它现在仍包含无效参数，而参数纠正排在 2-5：

- Yandex 的 rpt、cbir_page。
- TraceMoe 的 cutBorders。
- SauceNAO 的 output_type。

我再次跑真实 Yandex 调用，输入合成覆盖值，原始输出仍然是固定值：

    YANDEX_EXTRA_REQUEST https://yandex.com/images/search?rpt=imageview&cbir_page=sites&url=https%3A%2F%2Fsource.invalid%2Fimage
    YANDEX_EXTRA_OUTPUT_OK True

这意味着“允许了一个键”不等于“支持了这个参数”。

开工前应至少列出本批承诺范围的参数名、类型、范围、初始化参数／search 参数归属，以及文档同步规则。不需要完整全引擎能力矩阵，但必须有这份有限契约。

第二，拒绝必须发生在文件读取和网络创建之前。

“没有出站请求”不足以证明没有读文件。读完文件再拒绝，也满足你当前这一句验收。

至少要覆盖顶层 null、数组、键值对数组、保留键、未知键、错误类型；断言没有到达读取函数和网络动作。

1-2：基本够格，可以开工

补两个精确条件即可：

- “退出”必须是非零退出。
- 故障注入只让 SSE 构造失败，不能把所有 http_app 调用都一起模拟成失败。

后者不是理论问题。本轮原始输出：

    ALL_HTTP_APP_CALLS_FAIL_EXIT 1 uvicorn_called= False

这在当前有漏洞的旧代码上就能通过。

只让 transport="sse" 失败，保留真实 fallback 后：

    SSE_ONLY_EXIT None uvicorn_called= True http_app_calls= [{'transport': 'sse'}, {'path': '/'}]

这才真正触发了你要删除的错误行为。

所以这条可以实施，但测试必须保留“旧实现会走通的后门路径”，否则测不到降级缺陷。

1-3：基本够格，可以开工；正向验收需要补齐

“匿名 POST / 不进入 MCP”是必要条件，但当前正常 SSE 应用就返回 405，因此单测这个条件不能证明根路径豁免已经收窄。

应同时验证：

- /healthz 的 GET／HEAD 匿名可用，HEAD 无响应体。
- 健康检查没有外部 HTTP 请求。
- 非健康路径不继承豁免。
- 非健康方法不能继承 GET 的豁免。
- 授权客户端仍能正常使用协议端点。

测试应覆盖真实装配应用，也要能单独验证中间件豁免条件。它与 1-2、1-5、1-9 共享认证装配，适合同一组集成测试。

1-4：目前不够格，是首批的设计阻塞之一

除了 Q2 中的 jar 接入问题，更关键的是凭据归属。

当前只有一个全局 IMAGE_SEARCH_COOKIES，README.md:89 也把它写成通用 cookies。它没有引擎归属信息。

如果实现者这样做：

    每次根据用户选择的 engine，
    给同一份全局 cookie 加上那个 engine 的 domain。

那么 A 引擎凭据仍然可能被发送给 B 引擎，只是变成“有域的错误凭据”。

必须在任务中确定旧配置的处理方式，例如：

- 引擎专属配置。
- 有明确 engine／domain 归属的配置结构。
- 对旧全局配置要求明确归属，归属不明则拒绝或禁用。

不必在评审中强选变量名，但不能让实施者自行猜凭据属于谁。

验收还要增加正向条件：

- 正确引擎能收到正确凭据。
- 其他引擎、无关域收不到。
- 用户图源下载始终不带引擎凭据，包括同域图源。
- 合法代理配置确实构造成功，环境变量优先级明确。

当前“非目标域无 Cookie”的验收，直接删除全部 cookie 功能也能通过。

当前“还原 dict 实现会失败”的反向测试，只守住类型问题，没有守住泄露问题。还要故意恢复“合法字符串＋无域 cookie”，确认安全测试失败。

1-5：部分够格；验收命令有实际错误

docs/ROADMAP.md:118 写“无 token 起 --host 0.0.0.0”。

但当前 main.py:55 只有 --sse 才进入 HTTP 模式；仅传 --host 仍走 stdio。实际输出：

    NO_TOKEN_ARGS ['--host', '0.0.0.0'] stdio_called= True uvicorn_called= False exit= None
    NO_TOKEN_ARGS ['--sse', '--host', '0.0.0.0'] stdio_called= False uvicorn_called= True exit= None

验收必须明确使用 --sse。

还要把行为矩阵定清楚：

- 默认 stdio 不需要 HTTP token。
- 普通 HTTP 模式缺 token 或只有空白，启动失败。
- 匿名 HTTP 必须显式开启开发模式。
- 开发模式只能绑定允许的回环地址，不能因为开了开发开关就允许 0.0.0.0 或 ::。
- 有效 token 的 HTTP 模式能正常启动和调用。

“公网/HTTP”容易让人只拦 0.0.0.0、不拦普通回环 HTTP。你旧意见其实更明确：HTTP 默认要求认证，匿名是显式例外。

1-6：目前不够格；五项是必须回答的问题，不是已经回答的实现约束

你把它从“只有测试”改成“实现任务”，这步是对的。

但第五章使用“谁接管”“如何保证”“如何限定”等问句，说明关键决策仍未完成。不是要求你写出整段实现代码，而是以下事项如果不定，实施者会合理地做出互不兼容、甚至不安全的方案。

至少补齐下面这些内容。

a. 实际接管入口

当前已接入引擎中，明确调用上游 download(url) 的有：

- EHentai：PicImageSearch/engines/ehentai.py:89–90。
- BaiDu：PicImageSearch/engines/baidu.py:98–99。

需要写清在什么位置将这些调用替换为安全下载，再将 bytes 交给引擎的 file 参数。

Yandex 的首次 URL 搜索应继续由 Yandex 抓图，不应为了“统一下载”而让服务器无差别下载所有 URL。Yandex 的本地下载只在后续明确允许的兜底路径发生。

不能安全接管的路径，本批应明确拒绝，而不是继续透传。

b. URL 与地址策略

明确：

- 允许的 scheme、端口、是否拒绝 URL 内嵌用户名密码。
- 哪些非公网地址被拒绝，覆盖 IPv4、IPv6、IPv4 映射 IPv6。
- DNS 同时返回公私地址时如何处理。
- DNS 失败时不得退回未校验连接。
- 公网 IPv6 正例要能通过，不能把“覆盖 IPv6”实现成“一律拒绝 IPv6”。

c. DNS 到实际连接的绑定方式，以及代理策略

“解析一下，确认公网，然后普通 client.get(url)”仍然可能二次解析。

需要规定实际连接使用已验证的目的地址，同时保留正确 Host、TLS SNI 和证书校验。

代理是这里的关键遗漏：如果代理替你解析目标域名，本机 DNS 校验并不能证明代理最终连接哪里。

应明确二选一或支持范围：

- 用户图源下载不继承环境／引擎代理。
- 走具有相同目的地址约束的受控代理。
- 暂不支持的代理组合直接拒绝。

不能把这个问题留给“按契约传代理字符串”解决。

d. 重定向规则

不只是“每跳校验”：

- 关闭不受控的自动跟随。
- 规定最大跳数。
- 每跳重新验证 URL、地址和凭据策略。
- 明确 HTTPS 降级到 HTTP 的行为。

e. 预算的数值、单位和计数位置

现在没有明确最大字节数、重定向次数、下载总期限。

应明确：

- Content-Length 只可提前拒绝，不能代替实际流式计数。
- 无 Content-Length、分块响应也必须限制。
- 压缩响应按哪一层大小限制，避免解压后突破预算。
- 超限后立即停止继续消费响应。
- 拒绝空体及不支持的图片类型，还是允许任意 bytes，必须决定。

总下载期限应覆盖 DNS、连接、重定向和读取。不能等到第三批才给第一批下载器一个可终止的总期限。

f. 生命周期和失败行为

超限、重定向拒绝、取消、超时后，响应和 client 都应关闭；不能留下挂起连接，也不能绕回不安全旧路径。

g. 验证到底测到哪一层

MockTransport 适合验证 HTTP 语义，但不会做真实 DNS 和连接。

“DNS 校验结果与连接目标一致”的测试必须观察受控 resolver／连接后端实际收到的目的地址，而不能仅在 MockTransport 里断言 URL 字符串。

再加正例，避免“所有下载都拒绝”通过安全测试。

这些内容可以形成一份很短的下载器契约。当前五个问题不足以替代它。

1-7：部分够格；是所有修复的伴随前置，不是第七个才做

应先有最小禁网和运行骨架，然后每个修复带自己的测试进入同一条运行路径。不必先完成一套庞大测试平台。

“恢复任一修复 → check.sh 非零”还应明确为：

    每一类修复都至少有一个针对性的反向验证。

只挑一个最容易测的缺陷恢复，不能证明其余门禁有效。

另外必须补 CI 实施条目。check.sh 是入口，不是自动执行机制。

1-8：修改目标够格；当前验收不够格

“git check-ignore tests/** 无输出”有三个问题：

- ** 的展开依赖 shell 配置，不保证递归检查全部文件。
- tests 下的 __pycache__ 本来就应该被忽略。
- git check-ignore 在没有命中时返回 1，直接塞进 set -e 的脚本可能把正确状态当失败。

本轮原始输出：

    .gitignore:34:test_logic.py	tests/test_logic.py
    .gitignore:36:integration_test.py	tests/integration_test.py

对正常测试名及缓存目录的检查：

    .gitignore:2:__pycache__/	tests/__pycache__/test_auth.cpython-311.pyc

应该验收“正式测试源码和 fixture 未被意外忽略、已进入版本控制”，而不是“tests 内任何东西都不能被忽略”。

1-9：部分够格；几项加固可立即开始，但认证矩阵还没定义

compare_digest、畸形头拒绝、WWW-Authenticate、WebSocket 明确拒绝，目标清楚。

缺的主要是策略：

- 允许哪些 Origin，如何配置。
- 没有 Origin 的原生 MCP 客户端允许工作。
- 非法／重复 Authorization 如何处理。
- token 字符集或字节比较方式，避免机械替换比较函数后引入新的异常。
- WebSocket 拒绝，而 lifespan 仍正常透传。

更重要的是，把这条纳入首批最终验收。现在 :124 遗漏了它。


Q4. 排序还有问题吗？

有，但不是把四批全部推倒重排。

一、你特意做的两个调整都正确

“2-1 错误分类先于 4-6 结构化输出”：对。

错误识别是行为基础，不需要等待完整业务 JSON schema。可以先有内部状态／原因和正确 MCP 错误语义，再做结果结构化。

“删静默降级前移到批次 1”：对。

这是低成本、高收益的安全修复，不应等待任何可用性重构。

二、首批白名单与第二批参数修复仍然错位

1-1 需要真实参数契约，2-5 却才处理其中已知错误。

应将这些最小参数修正与 1-1 同步：

- cutBorders 的对外处理策略。
- output_type 固定 JSON／禁止覆盖。
- 删除或明确拒绝无效的 Yandex rpt、cbir_page。

结果展示中的零值、相似度等仍可留在第二批。不要为了统一编号，强行把“输入契约”和“展示修复”绑在一起。

三、下载器自身期限不能等第三批

3-1 的“整个工具调用总期限”和有界并发，可以作为统一可靠性任务推进。

但 1-6 下载器必须在第一批就有自己的有界下载、重定向和终止规则。流式限大小不会阻止一个永远慢慢发送、始终不超过大小上限的响应。

所以这里应拆成局部安全期限先做，工具级统一期限再整合。

四、首次安全上线不应等待新引擎和结构化输出

docs/ROADMAP.md:206 把首次替换放在批次 4 后，:210 又说每批在 us-01 真跑。

这取决于“真跑”的含义：

- 如果只是隔离环境验证，没有矛盾，但需要写清不替换生产。
- 如果包含线上替换，则锁定依赖、同一产物验证、TLS／回环边界、低权限运行和回滚能力都必须前置。

不能一方面每批替换生产，另一方面第四批才证明部署和回滚可靠。

也不应让已知漏洞的首次修复上线等待 3-4 新引擎、4-6 结构化结果。建议把最小发布门槛作为每次上线的闸门，而不是第四批末尾的一次性工作。

五、配置文档不能全部留到 4-7

1-4 会改变 cookie 的配置语义；1-5 会改变 HTTP 启动行为。

它们的迁移说明、示例和错误提示必须随修改一起交付。否则执行第一批后，README 仍指导用户使用失效或不安全配置。

第四批可以做文档总整理，但不能延后行为变更所需的文档。

六、依赖图需要表达真实关系

当前图把 2-1、3-1 画在“批次1”的树枝下，容易误读为它们已经包含在第一批。

建议明确表达：

    最小测试骨架＋CI → 各修复及其回归
    最小参数契约 → 1-1＋对应参数公示
    凭据与出站策略 → 1-4＋1-6，共同发布
    1-6＋可执行的错误识别＋3-1 → 3-2
    2-1／2-2 → 4-6
    最小部署、产物烟测、回滚验证 → 任何一次生产替换

1-4 与 1-6 的“同批”应是共同发布条件，不要求同一个提交，更不意味着其他独立安全修复都不能先开始。


Q5. 你这次自己的复验可靠吗？

五项核心实验结论都复验成立。第一项需要限定“只发目标域”的含义，第五项不能延伸成中文标题或线上服务健康证明。

本轮实际版本：

    VERSIONS {'PicImageSearch': '3.12.11', 'httpx': '0.28.1', 'fastmcp': '4.0.10', 'mcp': '2.3.0'}
    IO_POLICY=deny file writes, DNS, sockets; HTTP=MockTransport

1. cookie 无域：对；“②与③必须共同修复后发布”的推论成立

原始输出：

    dict https://yandex.com/a sid=SYNTHETIC_ENGINE_COOKIE
    dict https://untrusted.invalid/b sid=SYNTHETIC_ENGINE_COOKIE
    dict https://api.trace.moe/c sid=SYNTHETIC_ENGINE_COOKIE
    dict https://images.yandex.com/d sid=SYNTHETIC_ENGINE_COOKIE
    dict http://yandex.com/plain sid=SYNTHETIC_ENGINE_COOKIE
    dict https://notyandex.com/e sid=SYNTHETIC_ENGINE_COOKIE

    scoped https://yandex.com/a sid=SYNTHETIC_SCOPED
    scoped https://untrusted.invalid/b None
    scoped https://api.trace.moe/c None
    scoped https://images.yandex.com/d sid=SYNTHETIC_SCOPED
    scoped http://yandex.com/plain sid=SYNTHETIC_SCOPED
    scoped https://notyandex.com/e None

真实 Network 每次新建，结果仍然泄露：

    fresh_Network https://untrusted.invalid/new1 sid=SYNTHETIC_ENGINE_COOKIE
    fresh_Network https://api.trace.moe/new2 sid=SYNTHETIC_ENGINE_COOKIE

因此，只修“dict 改字符串”确实会让 cookie 开始进入原先被构造失败挡住的请求路径。

两个限定：

- domain="yandex.com" 也会匹配其子域；不是只匹配一个精确主机。
- 这里用 Cookies.set 设置的 cookie 仍会发到 HTTP，不自动具有 Secure 限制。

所以“限域有效”成立；“这一行已经完成凭据安全隔离”不成立。

“必须同批”也不是形式上的同一个提交。若暂时做不完完整下载器，可以先禁用无法安全处理的路径；不能开放带无域凭据的任意下载路径后再等下一批补救。

2. cutBorders 无效，正确 Python 参数名是 cut_borders：对

以下均通过真实 _search_image_logic → TraceMoe.search：

    EXTRA {"cutBorders": false} REQUESTS [('POST', 'https://api.trace.moe/search?cutBorders=true&url=https%3A%2F%2Fsource.invalid%2Fpicture.jpg'), ('POST', 'https://trace.moe/anilist')]

    EXTRA {"cut_borders": false} REQUESTS [('POST', 'https://api.trace.moe/search?url=https%3A%2F%2Fsource.invalid%2Fpicture.jpg'), ('POST', 'https://trace.moe/anilist')]

    EXTRA {"cut_borders": true} REQUESTS [('POST', 'https://api.trace.moe/search?cutBorders=true&url=https%3A%2F%2Fsource.invalid%2Fpicture.jpg'), ('POST', 'https://trace.moe/anilist')]

你的表述准确：False 时“不再携带该参数”，不是发送 cutBorders=false。

如果对外保留旧名字作别名，也可以，但必须显式转换并测试；不能继续把旧名字直接丢给 **kwargs。

3. Lenso 构造即弃用警告：对

原始输出：

    class PicImageSearch.engines.lenso.Lenso
    DeprecationWarning: The Lenso engine is deprecated as the website now uses Cloudflare turnstile protection which prevents this client from working properly.

探针显式启用了 warning 捕获。默认运行时不一定在终端显示 DeprecationWarning，但 warnings.warn 确实执行了。

这足以支持“不把它列为本轮接入收益”。不等于本轮另行完成了一次在线可用性测试。

4. AnimeTrace 用现有格式化器返回空字符串：对

使用真实 AnimeTraceItem，不是自造属性对象：

    box [0.1, 0.2, 0.3, 0.4] box_id SYNTHETIC_BOX characters [Character(name='SYNTHETIC_CHARACTER', work='SYNTHETIC_WORK')]
    formatted ''

模型对象字段 characters 的判断正确。顺带提醒，原始 JSON 在这里用的是 character；接入时不要把模型字段名直接当作响应 JSON 的键。

5. TraceMoe 标题不是永远为空：对

没有模拟 TraceMoe.search 或 update_anime_info；只提供外部 HTTP 合成响应，并在真实格式化器外围观察结果。

原始输出：

    ANILIST_REQUEST id=123 query_contains_chinese=False
    FINAL_TITLES ('SYNTHETIC_ENGLISH', '')
    OUTPUT_BEGIN
    Search Engine: TraceMoe
    Found 1 results (showing top 1):

    --- Result 1 ---
    Episode: 1
    Time: 0.0s - 12.5s
    English Title: SYNTHETIC_ENGLISH
    Romaji Title: SYNTHETIC_ROMAJI
    Native Title: SYNTHETIC_NATIVE
    Filename: synthetic-episode.mkv
    OUTPUT_END

实际补资料请求发往 https://trace.moe/anilist。

这足以推翻“标题永远为空”。它证明完整链路会消费补资料响应，不证明远端今天一定可用，也不证明每个番剧都有所有语言标题。

这些受控探针的审计输出均为：

    BLOCKED_IO_ATTEMPTS []


Q6. v2 还漏了什么？

仍有几项“写了目标，但没写落实动作”，以及上一轮明确提出、这版尚未安排去留的内容。

一、最小 CI 实施任务

这是最明确的同类遗漏。

- 1-7 只创建本地测试入口。
- 4-5 说不要把发行卫生与“安全测试接入 CI”绑在一起。
- 4-7 要写真实 CI 结果。
- 第七章要求配置建议在 CI 执行。

但任务表里没有一条负责配置 CI、调用 check.sh、安装受测依赖并让失败阻止发布。

这不是措辞问题，必须有实施落点，且属于首批。

二、凭据配置迁移

1-4 承诺按引擎隔离，但没有定义旧全局配置的归属和迁移。

应把配置结构、旧值处理、错误提示、文档示例一起分配给 1-4。否则很容易实现成“同一份 cookie 换着 domain 发”，表面限域、实质仍串引擎。

三、候选升级验证的自动执行方式

2-4 已有任务，不算完全遗漏，但仍只有“运行测试”的目标：

- 如何得到候选组合。
- 测最低受支持组合、生产锁定组合，还是最新允许组合。
- 何时执行。
- 失败通知谁、是否阻止更新。

不补这些，就不能声称“上游改了会报警”。只在人工想起升级时跑一次测试，提供的是升级门禁，不是主动预警。

四、最小耗时基线

docs/ROADMAP.md:176、:237 承诺“先补最小耗时基线”，任务表没有落点。

可以很小：选定输入和环境，记录总耗时及必要阶段耗时，注明是否包含远端请求。无需立刻引入监控平台，但要分配到某一条。

五、参数公示修正漏了 Yandex

2-5 写了 TraceMoe、SauceNAO，却没有明确处理已经确认无效的 rpt／cbir_page。

2-3“加测试”可能把它们测红，但不是修复任务。应明确删除无效公示／拒绝无效输入，并与 1-1 同步。

六、依赖与支持边界仍有若干明确缺项

可以归到 2-4／2-7，不必另开大工程：

- main.py 直接导入 Starlette，应明确声明直接依赖。
- requirements.txt 与 pyproject.toml 的关系应确定，避免双份手工维护。
- 声明支持 Python >=3.10，就要有相应兼容验证，而不只是当前 3.11 环境。
- server.py:8–11 的失效导入兼容分支尚无明确删除或支持计划；删 main.py 的协议降级不等于这个分支也消失。
- 协议测试需要明确当前承诺的传输与协议范围。

七、几个小目标没有完整收口

- TraceMoe 的 similarity、image／video 映射，在旧评审中已指出，v2 没写修复或明确延期。
- 2-2 说详细 traceback 留服务端，但没明确日志脱敏；详细异常可能包含请求 URL 和配置片段。
- “先修轮换”出现在不做 OAuth 的理由里，任务表没有轮换操作与验证安排。若只是后续原则，就别写成本轮承诺。
- 2-6 仍需明确预算值、单位和超限行为；“有预算”不是可判定验收条件。

最该改的三处

第一，补完 1-4／1-6 的联合安全网络契约。

至少明确凭据归属、jar 接入点、无凭据图源 client、实际接管路径、DNS／连接绑定、代理策略和下载预算。

不改的后果：正常配置修复后把泄露打开；或者新增了一个安全下载器，但真实引擎仍绕开它；又或者所有下载都被拒绝，安全测试却宣布通过。

这是最硬的阻塞。

第二，把首批门禁写成能排除错误实现的验收，并真正接上 CI。

补正向功能用例；每类缺陷都有针对性反向测试；1-9 纳入最终验收；修正 SSE 故障注入、HTTP 启动命令和 .gitignore 判定。

不改的后果：保留旧降级分支、删除全部 cookie 功能、甚至没有自动执行测试，都可能被当作“首批验收通过”。

第三，先定首批参数契约，别让 1-1 等第二批才知道哪些参数有效。

把已知无效参数的处理与白名单、公示同步交付。结果展示小修可以继续留在第二批。

不改的后果：白名单会把错误公示固化成“受支持 API”；第一批刚完成，第二批就被迫重新解释接口、改测试和改文档。

最终核对输出：

    REVIEWED_FILES 9 HASH_MISMATCHES []
    ?? docs/

docs/ 原本就是未跟踪状态，本轮没有改变它。

总结：v2 不能原样作为“无需再问即可完整执行”的任务包；独立认证修复和测试骨架可以立即开工，但 1-4／1-6 尚未闭合的凭据归属与安全下载契约，是网络修复开工和首批安全验收的硬阻塞。
ASTRA_EXIT=0
