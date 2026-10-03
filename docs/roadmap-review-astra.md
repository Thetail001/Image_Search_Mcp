1. 已写下的判断，哪些是错的

最严重的排序错误：你把已经证实的条件性认证绕过留到了 P3，却把参数说明和 .gitignore 放进“必须立刻做”的 P0。main.py:66–72 的降级分支既不需要 compat 层，也不需要可靠性重构才能删除，没有理由等到第四批。

本次复用了已有环境：PicImageSearch 3.12.11、FastMCP 4.0.10、MCP 2.3.0、HTTPX 0.28.1。探针使用真实模型、真实引擎和真实 ASGI 装配，只模拟外部 HTTP 响应；没有访问生产服务，没有修改、新建或删除文件。检查范围内的源码和三份文档，执行前后内容摘要一致。下文 server.py、main.py 均指 src/image_search_mcp/ 下的文件。

“靠猜，所以升级必坏”：证据不足，诊断过度。

我读了那段提交的实际 diff，不只是标题：

    eb8d4e2：handle_messages → handle_post_message
    c78b782：改用原始 ASGI handler 和 Mount
    27eb5b0：mcp._server → mcp.server
    1ec6b21：显式指定 transport="sse"

这些证明发生过接口误用、错误假设和反复修补；不能证明作者没有查资料，也不能把每一次适配修正都归为猜测。尤其“升级必坏”没有成立：当前 FastMCP 4 的主路径仍能构造正常 SSE 应用，Yandex 也健康。

docs/ROADMAP.md:80 应改为可以验证的病因：

    没有明确的受支持依赖组合，没有覆盖真实应用装配的测试，
    接口误用和失效兼容分支能够进入发行物。

这比评价开发者当时怎么想更有用。

“.gitignore 必须第一件，否则测试提交不上”：这条计划是错的。

实跑：

    git check-ignore -v --no-index \
      tests/test_logic.py tests/integration_test.py \
      tests/test_network_config.py tests/test_auth_asgi.py tests/conftest.py

只有前两个名字命中。它没有忽略整个 tests/，后三个正常测试文件名不受影响。

docs/ROADMAP.md:178 把一个特定命名陷阱扩大成了全局阻塞。应在加入首批回归测试时顺手修掉，不值得独占 P0 条目，更不应排在漏洞前面。我上一轮写的是“新测试可能根本没提交”，不是“所有测试都提交不上”。

“TraceMoe 标题硬编码空串，所以需要修结果映射”：当前证据不能支持这个结论。

这是本次额外发现的一条明确误判，位置在 docs/ROADMAP.md:133。

PicImageSearch/model/tracemoe.py:100–103 的确把标题初始化为空，但实际搜索随后执行：

    engines/tracemoe.py:228
    await asyncio.gather(...update_anime_info(...))

补资料函数在 :144–146 写入 native、romaji、english 标题。

我让真实 _search_image_logic → TraceMoe.search 完整运行，MockTransport 分别提供搜索响应和 AniList 响应，实际输出包含：

    English Title: SYNTHETIC_ENGLISH
    Romaji Title: SYNTHETIC_ROMAJI
    Native Title: SYNTHETIC_NATIVE

你验到了模型初始化状态，没有验到搜索结束状态。不能据此安排一项“修复永远空的标题”。中文标题是否可取得、补资料服务现实中是否失效，是另外的问题；这个离线探针不证明它们在线健康。

“补齐三个白捡引擎，一行映射表”：这条计划是错的。

docs/ROADMAP.md:132 低估了工作量，其中 Lenso 更不该列为可用性收益。

已安装的 3.12.11 中，PicImageSearch/engines/lenso.py:15–18 已明确标记弃用，构造时实跑得到：

    DeprecationWarning:
    The Lenso engine is deprecated as the website now uses
    Cloudflare turnstile protection ...

这和 P2-3 要摘掉不可用引擎直接冲突。

AnimeTrace 也不能只加映射。我用真实 AnimeTraceItem 构造角色结果：

    characters = [Character(name='SYNTHETIC_CHARACTER',
                            work='SYNTHETIC_WORK')]
    当前 _format_result_item(..., "AnimeTrace") → ''

它的业务字段是 characters、box 等，现有格式化器不认识。类能 import，离“接入一个可用引擎”还差参数、输出、错误处理和验证。

“不跟上游 main，Bing 在 compat 层自己修”：前半句合理，后半句不划算。

不直接追随正在迁移 HTTP 栈的 main，符合当前 Yandex 优先的目标。但“不能取任何 commit，否则必然吃下整套迁移”过于绝对。审阅并单独移植一个修复，和安装整个 main，是不同决策。

自己实现 Bing 修复，意味着自己维护签名解密、解析样本、异常路径、上游页面变化，以及补丁来源和许可说明。放进 compat.py 不会消除这些成本，反而容易把适配层养成半个上游分叉。

我的选择：当前先标记该版本的 Bing 为不支持或实验性，不承诺自修。确有 Bing 使用需求后，再比较小范围回移植、受测 fork 和等待正式版本。

“先正确，再连接复用”：结论可以保留，理由要改。

共享 client 并不天然造成跨域 cookie 泄露。当前真正的问题是 cookie 没有域限制。

本次实跑：

    每次新建 Network(cookies="sid=SYNTHETIC_ENGINE_COOKIE")
    请求 untrusted.invalid
    → Cookie: sid=SYNTHETIC_ENGINE_COOKIE

    同一个 HTTPX client，cookie 明确限定 yandex.com
    请求 yandex.com       → 带 cookie
    请求 untrusted.invalid → 不带 cookie

因此，每次新建 client 并没有提供你想象中的隔离。P0-2 一旦修好字符串传参，已有问题就会暴露。

共享方案确实要处理同域不同凭据、可变 headers、代理配置和生命周期；这些是可控的工程风险，不是永久禁止复用的理由。当前没有延迟和连接成本测量，暂不优化合理；补一个最小耗时基线即可，不要用“正确性”代替性能判断。


2. compat.py：我的选择是不加当前计划中的这一层

不是按“384 行够不够资格”判断。问题在于，这个服务本来就是 PicImageSearch 的适配器，再加一个全面兼容层，目前没有显示出独立收益。

当前第三方调用已经集中在 server.py，核心入口是 _search_image_logic。把引擎映射、Network 创建和结果读取整体搬进 compat.py，可以改善文件组织，但不会自动形成更强的契约。

尤其这两条有问题：

    docs/ROADMAP.md:115
    用版本号做能力判断

    docs/ROADMAP.md:188
    compat.py 之外没有 import，即“契约自持”

版本号可以用于诊断和支持范围检查，不能代替行为验证。同一个安装版本不会告诉你远端网页是否变了；一个更新版本也不能自动证明某个字段或接口可用。

“唯一 import 点”只是结构约束。你完全可以把错误的 dict 传参搬进去，然后完美通过这个验收。

不加这一层，仍然可以做到：

    直接测试现有 _search_image_logic。
    Network 和引擎类用真的，外部响应走 MockTransport。
    断言具体请求字段、结果映射和错误分类。
    锁定生产组合，对候选依赖升级运行同一套测试。
    真实 FastMCP 客户端验证工具发现、调用和失败语义。

其中，“候选依赖升级跑测试”必须进入计划。永远只测锁定版本，不会因为上游发了新版本而自动报警。离线固定 fixture 也发现不了远端网页变化，需要另设低频在线抽验。

新增 compat 的成本不只是多跳一次函数：

    要维护应用语义、适配接口、上游接口三套边界。
    若结果对象仍向外泄露，隔离只是表面成立。
    若全部转换成自有模型，就提前承担了结构化结果设计。
    版本分支和旧版兼容路径会增长。
    fixture 和兼容代码可能共享同一个错误假设，一起绿。

我的选择会在以下条件下反转：

    确实需要同时支持存在行为差异的多个上游版本或替代实现；
    已经出现无法集中处理的重复兼容逻辑；
    或需要让安全网络策略在多个调用入口强制执行，
    并且能定义一个小而稳定的自有结果接口。

到那时加一个薄适配模块合理。现在不把它作为 P1 的交付目标，更不让它阻塞安全修复和 Yandex 兜底。即使以后加，直接导入第三方库的契约测试也应豁免“唯一 import 点”规则。


3. 三项待复验：实跑结论

3.1 episode=0、To=0：成立，保留 P2 小修

使用真实 TraceMoeItem，而非自造属性对象：

    输入：episode=0, from=0.0, to=0.0

    实际输出：
    Time: 0.0s - ?s
    Filename: synthetic-episode.mkv

    对照：episode=1, from=0.0, to=12.5

    实际输出：
    Episode: 1
    Time: 0.0s - 12.5s
    Filename: synthetic-episode.mkv

结论：

    episode=0 被 server.py:190 的真值判断吞掉。
    To=0 被 server.py:197 显示为问号。
    From=0 没丢，因为使用了 is not None。

改成明确区分 None 和零即可。它是可复现的展示缺陷；探针没有证明现实响应中 To=0 很常见，不值得升为安全或可靠性主任务。

3.2 SauceNAO 相似度：成立，保留 P2

真实 SauceNAOItem 解析后：

    item.similarity = 87.65

交给 SauceNAO 格式化分支，输出只有标题、URL、缩略图、作者和外链，没有 Similarity。把同一对象交给 Iqdb 格式化分支作为对照，则出现：

    Similarity: 87.65%

我还通过真实 SauceNAO.search 跑了完整 _search_image_logic，结果相同。丢失发生在本项目 server.py:181–187，不在上游解析器。

这一项应与 TraceMoe 相似度、零值一起做一组小型结果映射测试，不需要等 compat 或结构化结果重构。

3.3 WebSocket：认证中间件漏处理成立，匿名调用工具不成立

通过真实 main() 装配应用，仅截获 uvicorn.run 避免开监听端口，再直接投递匿名 WebSocket ASGI scope：

    /             websocket.close，未 accept
    /sse          websocket.close，未 accept
    /sse/         websocket.close，未 accept
    /messages     websocket.close，未 accept
    /messages/    KeyError: 'method'，未 accept
    /mcp          websocket.close，未 accept

HTTP 对照：

    匿名 POST /sse       → 401
    匿名 POST /messages/ → 401
    匿名 POST /mcp       → 401

另用一个下游哨兵确认，AuthMiddleware 确实会原样放过 websocket scope。

准确结论：请求绕过了中间件的认证分支，但当前应用没有可用的 WebSocket 工具路由，因此没有证据支持“通过 WebSocket 匿名搜索”。

建议保留显式拒绝 WebSocket，作为低成本稳健性修复，防止 /messages/ 进入错误处理路径。不要把它提升成第二个已证实的认证绕过漏洞，也不必删除这项防护。

另外，limit 也顺手复验了：

    limit=-1 → Found 3 results (showing top -1)，实际输出 2 条
    limit=0  → Found 3 results (showing top 0)，仍发出 1 次搜索请求
    limit=1  → 输出 1 条

因此负数缺陷可以去掉“待复验”。但 limit 的合法范围，不能替代下载和输出大小预算。


4. 序列与粒度

P0 四条不能按现在的说法视为互相独立。

第一，P0-1 的参数白名单需要先确定真实可用参数；P0-3 又在修改公示参数。两者可以分工，但必须共享一份已经核实的参数契约，否则会出现“文档允许、白名单拒绝”或“白名单保留了无效参数”。

第二，P0-2 与 cookie 隔离存在发布依赖。上一轮评审 :112–117 已经明确指出：错误的 dict 传参暂时遮住了无域 cookie 的泄露。现在计划把前者放 P0，把后者放 P1-4，而且后者只有测试，没有明确实现任务。这不是安全的分批边界。

第三，P0 已经要求真实 Network 测试、逐引擎契约测试和反向测试，但测试骨架安排在 P1。docs/ROADMAP.md:103–107 与 :116–119 在执行顺序上自相矛盾。

P0 应包含最小测试设施和对应回归，不必包含完整 P1 测试体系。

“P0 单独落一批”本身正确；“P0 没有持久化门禁”不可取。

应当一起落的是：

    修复 + 对应回归测试 + 默认禁网 + 一条自动执行入口。

不必等覆盖率体系、所有引擎 fixture、完整契约矩阵做好。临时人工探针能证明当下修好了，但不能防止下一次发行又坏；这个项目恰好已经吃过这种亏。

还有一个验收歧义：docs/ROADMAP.md:107 要“三组复现全部不再成立”。如果指前文 A/B/C，P0 根本没有修 C，验收不可能通过；如果仅指 cookie、proxy、file 三个探针，必须逐项写明，不能造成认证洞已经关闭的错觉。

P2-2 依赖 compat：这是人为添加的前置。

PicImageSearch/engines/yandex.py:40–44 已经公开提供 url 和 file 两个参数。本项目也已经会把 Base64 解成 bytes 后传 file。兜底不需要接触引擎内部。

它真正依赖的是：

    安全下载器：无引擎凭据、限制目的地址、重定向和下载大小。
    明确触发条件：抓图失败、允许尝试的空结果、配额或 CAPTCHA 分开。
    统一总期限：包含第一次搜索、下载和上传搜索。
    最多兜底一次，并向调用方说明实际使用的模式。

尤其“0 条”不等于“图源抓不到”。可以选择对空结果尝试一次上传，但这是带成本的产品策略，不能把所有空结果都诊断为抓取失败。

最可能做完没用的是 P2-4：Lenso 已弃用，AnimeTrace 只加映射会输出空内容。

最容易拖成长期工程的是“所有引擎、所有参数、所有能力一口气验完”。P1-3 应先覆盖实际承诺支持的范围。请求测试还应断言具体字段的正确变化，不能只比较整个请求是否不同，multipart 随机 boundary 就可能制造假阳性。


5. 最大的遗漏

最大的遗漏是：安全图片下载和凭据隔离只有测试条目，没有明确的实现任务。

docs/ROADMAP.md:118 写了拒绝私网、拒绝私网重定向、图片请求不带 cookie，但没有交代：

    谁接管上游引擎内部的 URL 下载？
    如何保证 DNS 校验结果与实际连接目标一致？
    每次重定向如何重新验证？
    引擎 cookie 如何限制可信域和引擎范围？
    下载怎样流式计数并在超限时停止？

上一轮评审 :119–125、:441–450 已明确提出这些要求。当前 PicImageSearch/network.py:332–333 会读取整个响应，单纯加总超时不能阻止大响应快速耗尽内存。

这项还直接影响 Yandex 上传兜底：原本由 Yandex 抓取 URL，兜底后变成你的服务器主动下载。可用性改进扩大了本机的出站访问面，必须先有安全边界。

另外几项没有完整收入计划：

输入、下载、输出预算。

上一轮 :203–213 的严格 Base64、合法 data URI、非空和大小限制，以及 :190–192 的输出预算，都没有明确落点。P2-1 的 deadline、P2-5 的 limit 不能覆盖它们。

两个已证实的错误参数。

P0-3 只修 Yandex，但上一轮 :257–259 还指出 TraceMoe 的 cutBorders 和 SauceNAO 的 output_type。本次再次实跑：

    extra={"cutBorders": false}
    → 请求仍有 cutBorders=true

    extra={"cut_borders": false}
    → 请求不再携带 cutBorders=true

    SauceNAO output_type=0，返回模拟 HTML
    → 被报为 CAPTCHA/API 变化，并建议配 cookie

加契约测试会让这些问题变红，但计划还需要写明怎么修，不能只安排“发现”。

真实 MCP 协议和发行物验证。

上一轮 :454–486 要求 stdio、HTTP/SSE、真实客户端、构建后的 wheel 安装测试。当前计划主要围着 PicImageSearch 转，恰恰漏掉了那串 FastMCP 修补提交对应的边界。

现代 HTTP 传输的显式选择与兼容迁移也没有落点。它可以延期，但应明确延期，并保留 SillyTavern 的真实客户端烟测。

错误语义被推得太晚。

isError 和错误分类写进了 P3-7，所以不算完全遗漏；问题是你把它们和完整结构化结果绑在一起。Yandex 兜底、引擎摘牌、健康监测都需要先区分 no_results、rate_limited、captcha、upstream_error。它们是前置能力，不该等 JSON schema 大改。

健康检查的职责还没分清。

docs/ROADMAP.md:143、:191 容易被实现成“每次 /healthz 都真实搜图”。这会消耗配额，让上游故障触发本机重启，甚至形成重启循环。

进程存活、本地就绪、外部引擎可用性应该分开。真实搜图放低频抽验，记录最近成功时间和失败原因；不要让无匹配结果直接等于进程不健康。


6. 如果只能做三件事

我选下面三个修复主题，每项都自带最小回归测试，不把测试另算成第四件：

第一，P0-1：严格限制 extras。

禁止保留键覆盖，拒绝非对象、未知键和非法类型，在创建网络和读取文件前失败。它阻断已经证实的文件外泄路径。

第二，提前做 P3-2，并修正根路径认证豁免。

删掉静默协议降级；构造失败直接退出；匿名 POST 不能因路径是 / 就被放行。用真实装配应用做回归。这个改动小，风险降低明确。

第三，P0-2 连同 cookie 安全边界一起修。

修好正常 cookie/proxy 配置，但不能把全局 cookie 原样发给任意图源。若完整安全下载器暂时做不完，先拒绝不能安全处理的 URL 下载路径，而不是带着泄露上线。

明确建议砍掉或延后的内容：

    砍掉当前 P1-1 的全面 compat 层及“唯一 import 点”验收。
    砍掉本轮自修 Bing 的承诺。
    从 P2-4 删除 Lenso，延期 AnimeTrace、Copyseeker。
    延期全引擎能力矩阵，先保证 Yandex 和公共边界。
    拆开 P3-7：错误语义和 traceback 清理提前，完整结构化结果延期。
    延期共享连接池优化，先收集实际耗时。
    不建设实时搜图型 /healthz，改为轻量健康端点加独立抽验。

LICENSE 等发行卫生可以在下一次发行时顺手补，但不应和安全测试接入 CI 捆成一个只能在 P3 完成的大条目。


我会怎么改这份计划

1. 改 :3、:80：删掉“升级必坏”和对作者“靠猜”的断言，改成“缺受测依赖范围与协议级发布门禁”。

2. 合并 P0-4 到首批测试提交；删除 :178 的“必须第一件事”。

3. 把 P3-2、缺 token 拒绝非开发 HTTP 启动，以及严格限定健康端点豁免，前移到安全批次。

4. 给 P0-2 增加发布条件：cookie 限域、限引擎，用户图源下载不带引擎凭据；不允许只修传参类型就上线。

5. 从 P1-2、P1-5、P3-8 抽出最小测试骨架和 CI，随 P0 一起交付。每项明确正常实现通过、恢复错误实现失败。

6. 删除当前 P1-1；把 P1 目标改成可执行的行为契约、受测版本范围和候选依赖升级验证。移除“没有外部 import 就算完成”的验收。

7. 把 P1-4 拆成“安全下载与凭据隔离实现”和“安全回归”，补 DNS/连接一致性、重定向、下载大小及输入输出预算。

8. 改 :179：Yandex 兜底依赖安全下载、错误分类和统一 deadline，不依赖 compat；最多尝试一次。

9. 改 P2-4、P2-5：删除 Lenso 接入；延期其余新引擎；撤回“TraceMoe 标题必为空”的结论；将零值、相似度、limit 标成已复现，并补 cutBorders、output_type 的具体修复。

10. 拆 P3：部署锁定和同一产物的客户端烟测放到首次线上替换之前；明确回滚验证。WebSocket 拒绝保留为小型加固；业务结构化结果和传输迁移另列后续批次。

这样调整后，首批结束才能准确地说“已知高风险路径被关闭，并且有测试守住”，而不是“目录结构更整齐，但认证洞还在等 P3”。
ASTRA_EXIT=0
