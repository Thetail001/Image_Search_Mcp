# 两个新增模块实现复核 A（astra）

## 复核基线与进度

复核范围：`09dda2e..9e6cb4d301af2035e012124a2fa04021173c44e5`，只评价 `src/image_search_mcp/safe_download.py` 与 `src/image_search_mcp/credentials.py`；测试、调用点和本机依赖源码只作证据。不修改实现，不访问外网，不运行全量 `check.sh`。

已实际执行：`git status --short; git branch --show-current; git rev-parse HEAD`。分支为 `audit/security-and-contracts`，HEAD 与任务书一致；开始时仅有未跟踪文件 `docs/impl-review-A-brief.md`，保持不动。两个模块在该区间均为新增文件，分别 481 行、267 行。

本机验证版本：Python 3.11.16、httpx 0.28.1、httpcore 1.0.9、PicImageSearch 3.12.11、pytest 9.1.1。生产运行情况采用任务书提供的信息；本复核不连接生产机器。

已完成两个模块、配套测试、必要调用点和本机依赖源码的复核。报告是在核查过程中分批落盘的；首轮疑点均已跟进：DNS 期限与同步解压峰值问题已复现，远程取图的默认值确认为 8 MiB，含额外等号的 cookie 值被错误拒绝。具体证据与修法见下文。

路径约定：未写完整目录的 `safe_download.py`、`credentials.py`、`server.py` 均指 `src/image_search_mcp/` 下同名文件；`httpx/`、`httpcore/`、`PicImageSearch/` 均指 `.venv/lib/python3.11/site-packages/` 下对应依赖。源码和运行结果只代表上述已核验版本，不把本机结果泛化到尚未检查的生产依赖副本。

## 1. 应当立刻修

### A1. 总期限没有包住 DNS、等待响应头和清理阶段

位置：`src/image_search_mcp/safe_download.py:298-309,399-406,414-439,459-475`。

原因：期限只在每跳开始、收到解码后的 body chunk 时比较；resolver 是没有 deadline 的裸 await。httpx 的各阶段超时是单次 I/O 超时，不是整条调用的绝对期限。读取循环也要等迭代器交出 chunk 才能检查，不能为响应头慢速发送、解码器暂不产出、EOF 等待或关闭过程提供完整的总期限保证。

实测：直接使用 `.venv/bin/python -c`，传入 `DownloadPolicy(total_timeout=.03)` 与一个等待 `asyncio.Event()` 的 resolver，用外层 `asyncio.wait_for(fetch(...), .15)` 保护探针。实际输出：`DNS_STALL TimeoutError None elapsed 0.15`。结束它的是探针外层超时，不是下载器的 `DownloadError(reason='timeout')`；全程没有网络请求。

建议：在 fetch 全流程外包一个绝对期限；项目支持 Python 3.10（`pyproject.toml:10`），可用 `asyncio.wait_for` 包内部协程，不要直接无条件引入仅 3.11 支持的 `asyncio.timeout`。分别处理自身期限、httpx 超时与外部取消，清理也要有界。补 DNS 停顿、响应头停顿、最后一个 chunk 后迟迟不 EOF、重定向累计耗时测试。系统 resolver 在线程池内执行时，取消等待不等于底层 getaddrinfo 线程立即终止，需要配合共享并发限制。

### A2. 解码后的计数正确，但解码过程能先冲破内存预算

位置：`src/image_search_mcp/safe_download.py:451-475`；依赖证据：`.venv/lib/python3.11/site-packages/httpx/_models.py:994-1003` 先 `decoder.decode(raw_bytes)` 再 yield；`httpx/_decoders.py:95-103` 的 gzip `decompress(data)` 没有限制输出长度。

实测：用流式 MockTransport 返回一个 32 MiB 重复字节压缩成的 gzip，先完成压缩再启动 tracemalloc，下载预算设 1 MiB。真实输出：

```text
GZIP 32636 wire bytes DownloadError too_large 响应超过上限 1048576 字节（已读取 33554432）
GZIP_PEAK_MiB 77.41
```

这不是 RSS 或生产峰值预测，是本探针的 Python 分配峰值；足以证明“1 MiB 最大响应”并不是“1 MiB 内存上限”。32 MiB 解码块已经生成才走到预算检查，错误返回虽然正确，OOM 防护却太晚。

建议：图片通常已压缩，最轻量方案是明确发送 `Accept-Encoding: identity`，并在读取前拒绝非 identity 的 Content-Encoding（只发请求头不够，服务端可能不遵守）。需要兼容压缩时，改用 `aiter_raw()` 加具有有界输出的解码器，同时限制压缩输入、解码输出和累计耗时。只加 `aiter_bytes(chunk_size=...)` 无法修复，httpx 是先解码后切块。保留一个有限大小的压缩回归样本和正对照，不制造真实 OOM。

### A3. IPv6 站点本地段 fec0::/10 被当成公网

位置：`src/image_search_mcp/safe_download.py:112-139`。没有检查 `IPv6Address.is_site_local`，而本机 Python 将这一旧站点本地段判成 `is_global=True`、非 private/reserved。

离线实测：`is_public_address('fec0::')`、`is_public_address('fec0::1')`、`is_public_address('feff:ffff:ffff:ffff:ffff:ffff:ffff:ffff')` 全为 True。因此 `resolve_and_validate` 会接受这些目标。危害条件是主机/网络仍有可达的旧站点本地路由；未证明当前美国 VPS 存在该路由，不声称已经能访问某个内网服务。但这个结果违反模块“只取公网”的契约。

建议：显式拒绝 `fec0::/10` 或 `is_site_local`，补段首、段尾和相邻公网段正反例；不要仅靠当前 Python 的 `is_global`。

### A4. cookie 格式校验拒绝合法值，且错误信息回显凭据值

位置：`src/image_search_mcp/credentials.py:73,118-123`；真实上游按第一个等号拆分：`.venv/lib/python3.11/site-packages/PicImageSearch/network.py:51-54`；错误对外返回路径：`src/image_search_mcp/server.py:377-381`。

实测：`normalize_cookie_string('sid=YWJjZA==', engine='Yandex')` 和 `'sid=a=b'` 都抛 CredentialError，错误文本包含完整原始 cookie 片段。反过来 `'bad name=x'`、含 NUL 的值被接受；非 ASCII 值直到构造 httpx 请求头才出现 UnicodeEncodeError。这里过严和过宽同时存在。

建议：使用 `partition('=')`，独立检查合法 token 名称和允许的值字符，允许值内等号、拒绝控制字符和不能编码到请求头的值。错误只写配置变量/片段序号/名称，不回显 value。当前生产 cookies 为空，尚未触发；这是启用新增 cookie 功能前应修的问题，不把它描述成现网正在泄漏。

### 首轮已验证事实（不以测试全绿替代正确性）

运行 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_safe_download.py tests/test_credentials.py`，实际输出 `95 passed in 2.03s`。禁用 bytecode 与 pytest cache，避免产生额外仓库文件。当前实际测试文件为 548 行与 267 行，`conftest.py` 为 152 行；任务书行数不是本次引用依据。

`server.py:329-336` 直接构造 `SafeDownloader()`，没有覆盖默认值。因此当前 HEAD 的远程取图上限确为 8 MiB（`safe_download.py:72`），不是 16 MiB。

真实 `Network` 的 jar 安装测试通过：`tests/test_credentials.py:207-224,250-257`。它验证了有域 cookie 发往本域且不发往无关域，不只是测试一个独立 helper。测试层仅替换 HTTP transport，不替换 Network 与 httpx cookie 实现（`tests/conftest.py:71-83`）。

尚不能据此宣布完整安全：MockTransport 接收的是请求对象，不是已建立的 TCP/TLS 连接；`tests/test_safe_download.py:145-160` 证实 URL/Host/SNI 参数正确，不能单独证明 TLS 校验或系统连接地址。

## 2. 值得做

### B1. 修正 URL 规范化、IPv6 authority 与失败类别

位置：`src/image_search_mcp/safe_download.py:153-162,165-195,326-340,367-379,417-436`。

实测结果：

```text
http://test.invalid:abc/x        -> ValueError，未包装为 invalid_url
http://test.invalid:65536/x      -> ValueError，未包装为 invalid_url
http://[bad/x                    -> ValueError，未包装为 invalid_url
https://例子.测试/x               -> UnicodeEncodeError（Host 未做 IDNA）
https://[2606:4700::1111]:8443/x  -> Host: 2606:4700::1111:8443（缺 IPv6 方括号）
302 -> https://[2606:4700::1111]/x -> 第一跳后 ValueError
ReadTimeout                    -> connection_failed
坏 gzip                        -> connection_failed
503 + Content-Length 超限      -> too_large
自重定向 /x -> /x               -> 两次请求后 redirect_loop
```

为什么：`urlsplit`、`.port` 自己能抛 ValueError；`Target.display` 丢失 IPv6 方括号，重定向下一轮重新 parse 就坏了；Host 手工构造未做 IDNA；所有 HTTPError 被统一当“无法连接”；HTTP 错误状态在读完 body 后才判断。调用方最终不会静默成功，但会返回过泛的内部错误或错误分类（`server.py:382-394`）。

建议：统一 URL/authority 规范化，保留 IPv6 方括号，主机先 IDNA；将解析异常显式映射到 invalid_url；先处理不可成功的 HTTP 状态；区分连接失败、读取/协议/解码失败和 timeout。初始化 visited 时纳入初始规范化 URL。补真实 IPv6 字面量/重定向测试，当前 IPv6 正例只覆盖“域名解析成 IPv6”（`tests/test_safe_download.py:163-169`）。

### B2. 加进程共享的下载/在途图片预算，别只依赖单请求大小

位置：`src/image_search_mcp/safe_download.py:277-287,412,457-475`；调用证据 `server.py:329-336,356-359,419-444`。这里没有共享 semaphore，且每次调用创建新的 downloader/client。给实例各自加 semaphore 没效果；httpx 每个池自己的连接限制也不是跨请求限制。

实测正常 8 MiB、64 KiB 分块响应：`BODY 8388608 CURRENT_PEAK_MiB (8.05, 16.07)`。`chunks` 加最终 `b''.join` 有短时双份缓冲；极小 chunk 的列表/对象开销另算。工具计算 50 份 payload：8 MiB 时 400 MiB；若调到 16 MiB 则 800 MiB。这不是整进程 RSS 上限，TLS、解压、上传、Python 对象和其他服务都还没算。也不能把单请求峰值简单乘 50 当成已测并发峰值：同一事件循环的同步 join 不是同时执行的。

建议：进程共享的准入预算覆盖下载、保留 bytes、引擎上传的整个持有期；若下载完立即释放 semaphore，图片仍会堆在等待上传的任务里。配置小的可调并发数、限制等待队列/等待时长，并根据实际 VPS 内存和压测定值。先修 A1/A2，再谈调整容量。当前调用方在回环且有 token，不把它等同公网匿名 DoS；但本机 agent 扇出同样会造成可用性故障。没有生产内存与负载数据，不能断言 50 并发必然 OOM，也不能声称 16 MiB 足够所有引擎。

### B3. 加轻量内容识别，避免把错误页上传给引擎

位置：`src/image_search_mcp/safe_download.py:390-394,421-436,348-351`。实测 `Content-Type: text/html`、body `<html>captcha</html>` 被原样返回。

这是正确性和诊断质量问题，不是绕过地址校验的证据；下载器本身没有把 HTML 当网页执行。建议拒绝明确的 HTML/JSON 错误页，结合受支持图片格式的有限魔数检查；允许可信的 `application/octet-stream`/缺失 MIME 图源走有限探测，别把严格 `image/*` 白名单当成充分或必要条件。若产品定义本来就是“受限通用字节下载器”，可以保留底层行为，但在交给图片引擎前完成验证。

### B4. 分清“同一引擎”与“同一 cookie 域”，保留 Secure 语义

位置：`src/image_search_mcp/credentials.py:48-59,128-142,164-165,257-267`。

实测只给 `google.co.jp` 建 jar 时，`google.com` 不带 cookie；只给 `google.com` 建 jar 时反向亦然。但用当前 Google 域名表构造 jar，两个域都收到同一个 `sid=FIXTURE`：这是 `for domain in domains` 主动复制，和浏览器公共后缀匹配无关。EHentai 的两个独立站点同理，应由产品/运维明确授权，而不是暗示保留了原始浏览器域作用域。

此外 httpx `.set` 默认 `secure=False`（`.venv/lib/python3.11/site-packages/httpx/_models.py:1117-1133`），实测 `http://yandex.com/` 也带 cookie；引擎 Network 自动跟随重定向（`PicImageSearch/network.py:65`），下载器的降级保护不适用于它。未发现现网实际 HTTP 降级，不说已经泄漏。

建议：支持按域配置或至少明确“一个引擎配置会复制给列出的全部域”；默认仅 HTTPS，必要时显式豁免。不要为返回的 CDN/头像 URL 扩大凭据域。`install_cookie_jar` 是合并不是清洗：已有无域 cookie 不会被它清掉；当前调用方创建干净 Network，满足前提，应把前提写入接口说明或校验。

### B5. “不使用环境代理”的承诺只在独立下载器上兑现

位置：`src/image_search_mcp/credentials.py:25-27,221-250`；集成证据 `server.py:313-326,436`、`.venv/lib/python3.11/site-packages/PicImageSearch/network.py:56-66` 与 `httpx/_client.py:1373,1399-1400`。

`load_proxy` 本身确实不读通用代理变量，但真实 Network 创建 httpx client 时没有禁用 `trust_env`。离线 spy 把 `httpx._client.get_environment_proxies` 替换为空字典，构造真实 Network：输出 `NETWORK_ENV_PROXY_LOOKUPS 1`。因此“警告说忽略了”不能保证底层真的忽略。SafeDownloader 显式 `trust_env=False` 是对的，不受此结论否定。

建议：在真正创建引擎 client 的边界禁用环境代理，包含 ALL_PROXY 一类来源；不要靠临时全局删环境变量修。相关调用点不在本次实现改动范围，本报告只定位跨模块契约。按给定生产配置此问题不是已发生的事故；若准备依赖“环境代理无效”的安全承诺，应在上线前完成修复。`load_proxy` 还会接受非数字/越界端口，畸形 IPv6 则直接抛 ValueError（离线输出见探针）；建议与 URL 入口一起校验并避免报错包含代理用户密码。

### B6. 补能在故意破坏实现时变红的测试

位置：`tests/test_safe_download.py:145-160,463-472,482-486`；`tests/test_credentials.py:46-60,173-183,236-247`；实现依赖 `httpx/_client.py:1399-1400`。

已做纯内存变异，不写仓库源码：把 `_request_pinned` 内的 `trust_env=False` 改成 True，同时从 Google 表移除 `google.co.jp`。只运行环境代理测试、逐引擎域名源码测试和引擎覆盖测试。实际输出：`13 passed, 1 warning in 0.04s`，warning 是先导入模块再启动 pytest 导致的 assert-rewrite 提示。

解释：显式 MockTransport 本来就让 httpx 不查询环境代理，故现有测试无法发现 trust_env 被删除。域名测试仅验证“表里的字符串出现在源码”，既不检测少登记域，也不区分代码与注释；google.co.jp 本身只是 Google 类文档示例。建议代理测试观察真实 client 的配置/环境查询，连接测试下沉到 httpcore backend，域名测试改为请求脚本与必要的动态响应链，并加入假阳性/假阴性反向测试。无凭据下载测试还应先给引擎装入假凭据，再经过 server 的取图输入路径确认没有复用；空 client 无 cookie 的断言覆盖面很窄。

此外上游 Bing 文件搜索在 `.venv/lib/python3.11/site-packages/PicImageSearch/engines/bing.py:102-108` 主动 `self.client.cookies.clear()`，`server.py:440` 的装载成功不等于全搜索过程都保留凭据。建议加引擎级功能回归，并先核实上游清空 cookie 的理由，别盲目覆盖它。

## 3. 对 15 个问题逐项回答

### Q1. 不检查 Content-Type 算不算问题？

算功能质量问题，归“值得做”（B3）。实测 HTML 验证码页会作为图片字节返回。仅检查 `image/*` 既可被伪造，又可能误拒 `application/octet-stream` 的真图片；应做有限类型识别而不是无界图像解码。没有证据表明当前路径在本机执行 HTML。

### Q2. 内存上限够吗？50 并发会怎样？

先纠正数值：远程下载是 8 MiB；16 MiB 是 `server.py:94-95` 的 Base64 输入限制，`server.py:336` 没把它传给 downloader。是否“够用”取决于引擎限制和真实图像分布，本次没数据，没把握。正常单次 8 MiB 下载探针峰值 16.07 MiB；50 份 8 MiB payload 就是 400 MiB，若统一到 16 MiB 则是 800 MiB，均不含额外对象和网络/引擎缓冲。压缩路径峰值还可远超这个估算，见 A2。建议明确统一或解释两个入口的不同预算，不能把 `max_bytes` 当进程内存上限。

### Q3. 没有并发上限要不要管？

要，归“值得做”（B2），但不凭空给出“50 一定撑爆”的结论。可见调用链没有共享准入控制（`server.py:329-336,419-444`）；每次新建 AsyncClient 的连接池上限不起全局限制作用。应让预算覆盖图片仍被上传任务持有的阶段，并给等待队列设界。回环调用和 token 降低匿名滥用风险，不能防 agent 扇出、上游变慢或代码重试叠加。

### Q4. 地址分类逐段核对

证据方法：使用当前 `.venv/bin/python` 直接调用 `is_public_address`，核对段首、段内和段尾；同时检查本机 `ipaddress._IPv4Constants/_IPv6Constants` 的所有 private/reserved/exception 表，并补充表外的转换及已废弃段。以下是本机 Python 3.11.16 的实际行为，不声称已经查过最新外部登记表；按要求没有访问它。项目只要求 Python >=3.10，这些表在其他解释器小版本可能不同，应将关键安全决策显式化并加版本矩阵。

IPv4：

| 范围/特例 | 当前结果 | 判定与既有覆盖 |
|---|---|---|
| `0.0.0.0/8` | 全段拒绝 | 不只拒绝 `0.0.0.0`；旧测试只测全零，新增探针含 `0.0.0.1`、`0.255.255.255` |
| `10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16` | 拒绝 | private；既有测试有典型地址，段边界由本次探针补查 |
| `100.64.0.0/10` | 拒绝 | CGNAT 的 private=False、global=False，靠最后的 `is_global` 拦；相邻 `100.63.255.255` 与 `100.128.0.0` 放行；既有测试遗漏 |
| `127.0.0.0/8` | 拒绝 | loopback/private；既有测试有 127.0.0.1 |
| `169.254.0.0/16` | 拒绝 | link-local，含元数据地址；既有测试有 169.254.169.254 |
| `192.0.0.0/24` | 除 `.9`、`.10` 外拒绝 | 本机表明确例外；`.8`、`.11`、`.170/.171` 均拒，`.9/.10` 放行。不能只拿 192.0.0.1 代表全段 |
| `192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24` | 拒绝 | 文档段；原地址单测遗漏 |
| `198.18.0.0/15` | 拒绝 | 基准测试段；`198.17.255.255`、`198.20.0.0` 放行；原地址单测遗漏 |
| `224.0.0.0/4` | 拒绝 | multicast；原测试只取段首附近 |
| `240.0.0.0/4`、`255.255.255.255/32` | 拒绝 | reserved/有限广播；后者原测试已有 |
| `192.88.99.0/24`（含 `.2`） | 放行 | 本机表未将它判为非 global，现实现也无单独政策；旧 6to4 中继特殊范围不能据“全保留都拒绝”一语带过。未证明有内网绕过，应明确是否整体拒绝转换中继范围 |
| `192.31.196.0/24`、`192.52.193.0/24`、`192.175.48.0/24` | 本机无否决，探针各取 `.1` 放行 | 特殊用途不等于私网，不能一概拒绝所有特殊地址 |
| 普通公网 IPv4 | 放行 | 既有 8.8.8.8/PUBLIC_V4 正例 |

这里的广播检查不等于识别一切“定向广播”：某个公网子网的尾地址是否是广播取决于路由/掩码，单个 IP 字符串不足以判断。不能宣称已经阻断任意公网子网定向广播。

IPv6 与包装地址：

| 范围/特例 | 当前结果 | 判定与遗漏 |
|---|---|---|
| `::/128`、`::1/128` | 拒绝 | 未指定/回环 |
| 旧 IPv4-compatible `::/96`，如 `::127.0.0.1` | 拒绝 | 由 reserved 兜住，不是显式拆 IPv4 |
| `::ffff:0:0/96` mapped IPv4 | 跟随内部 IPv4 判定 | `::ffff:127.0.0.1`、CGNAT 拒，`::ffff:8.8.8.8` 放；显式 unwrap 在所有 IPv6 flag 前返回（`safe_download.py:123-126`）。既有测试只覆盖 mapped 回环 |
| `::ffff:0:127.0.0.1` 等非 mapped 兼容形态 | 拒绝 | 不命中 ipv4_mapped 时由 reserved 拦；不要把它与 mapped /96 混淆 |
| `2002::/16`（6to4） | 取内嵌 IPv4 后决定 | `2002:7f00:1::` 拒，`2002:0808:0808::` 放；后者虽然本机 stdlib 已标成 private，代码显式提前返回覆盖了它。不是无条件拒绝 6to4，也不是根据最后 32 位判定。建议明确是否还需要支持转换地址，并固定政策 |
| `2001::/32`（Teredo） | 全部拒绝 | 归入 `2001::/23` private，无例外覆盖该 /32；未显式拆 server/client 不构成当前实现的绕过。原测试没有 Teredo |
| `64:ff9b::/96`（标准 NAT64 前缀） | 全部拒绝 | 内嵌公网和私网都拒：即使 `is_global=True`，也会先命中 `is_reserved`；不能误报为 NAT64 私网绕过，但公网 NAT64 也不受支持 |
| `64:ff9b:1::/48`（本地用途转换） | 拒绝 | private/reserved；原测试没有 |
| `100::/64`（discard-only） | 拒绝 | private/reserved；相邻 `100:0:0:1::` 也因更大的 reserved 范围被拒 |
| `2001::/23` 的其余部分 | 通常拒绝，例外如下 | `2001:1::1/128`、`2001:1::2/128`、`2001:3::/32`、`2001:4:112::/48`、`2001:20::/28`、`2001:30::/28` 是本机表的放行例外；`2001:1::3` 当前仍拒。例外已逐个探测 |
| `2001:db8::/32`、`3fff::/20` | 拒绝 | 文档范围；原测试没有 |
| `fc00::/7` | 拒绝 | ULA；原测试有 fd00::1，但没有全段边界 |
| `fe80::/10` | 拒绝 | link-local；原测试有 fe80::1 |
| `fec0::/10` | 放行，缺口 | 已废弃的 site-local，见 A3；遗漏 `is_site_local` |
| `ff00::/8` | 拒绝 | multicast，IPv6 本身没有 IPv4 那种广播 |
| `5f00::/16` | 拒绝 | 被更大 reserved 范围覆盖，不是独立业务规则 |
| 正常公网 IPv6 | 放行 | 2606:4700、2001:4860、2620:4f:8000 等正例；公网 host 附 `%zone` 也会放行，因为分类前直接去掉 zone（`:119`），建议 URL 层明确是否接受 zone |

还逐一检查了本机 stdlib 的全部宽 reserved 范围：`::/8`、`100::/8`、`200::/7`、`400::/6`、`800::/5`、`1000::/4`、`4000::/3`、`6000::/3`、`8000::/3`、`a000::/3`、`c000::/3`、`e000::/4`、`f000::/5`、`f800::/6`、`fe00::/9`。段首、下一地址和段尾的探针均为 False；这不否认 `::/8` 内 mapped IPv4 的显式提前返回例外，详见上表。

不保证所有“本机有特殊路由的公网前缀”都安全：自定义 NAT64 前缀、VPN/策略路由把全球地址送进内部服务，是部署路由信任边界，不能仅用 ipaddress 穷尽识别。没有读取生产路由，也没有证据说生产存在这种情况。

既有测试实际集中在 `tests/test_safe_download.py:109-129` 的常见内网/回环/公网样例，以及 `209-216` 的混合解析拒绝；上表的 CGNAT、6to4、Teredo、NAT64、完整保留段、site-local、各例外和边界大多未进回归。本次临时探针补查了它们，但没有擅自修改测试文件。

### Q5. 解析后按固定 IP 连接，是否杜绝二次解析换地址？

默认 transport 下，这个判断成立，但只覆盖 DNS 名字重绑定，不等于验证任意网络路由。`safe_download.py:258-262,388-410` 使用已验证 IP 构造 URL；httpcore 在 `.venv/lib/python3.11/site-packages/httpcore/_async/connection.py:115-124` 以 origin host 连接，并在 `149-156` 单独取 `sni_hostname` 做 TLS。

本次保留真实 httpx/httpcore，只替换最底层 `AutoBackend.connect_tcp` 为离线记录器，实际输出：

```text
BACKEND_CONNECT 8.8.8.8 443
TLS_ARGS images.example.com True 2
HTTP_WIRE_HOST ['Host: images.example.com']
DEFAULT_TRANSPORT_RESULT b'OK'
```

TLS 参数中的 True 是 check_hostname，2 是 CERT_REQUIRED。这验证了真实库的参数传递，不是一次真实 TLS 握手；任务书提供的实网证书验证结果与它相容，本次没有重新联网复验。

换 `transport_factory` 就不能保证：自定义 transport 可以忽略 IP/Host/SNI，启用自己的代理或 `verify=False`；`AsyncClient(verify=True, trust_env=False)` 不会重新配置调用者已经建好的 transport（`:409-410`）。应把它明确为受信任的测试注入接口，或限制生产可注入的实现。不能用“任意 factory 都保证 pin”作契约。

### Q6. 重定向路径完整吗？

主干完整，边缘有 bug：仅 301/302/303/307/308 进入跳转分支（`:54`），关闭 httpx 自动跳转（`:401`），用 urljoin 处理相对 Location（`:326`）；下一跳重新做协议、userinfo、DNS 和全部地址检查（`:308-309`）；HTTPS→HTTP 每跳拒绝（`:327-335`）。默认最多 3 次重定向，即最多首次加 3 跳；现有 max_redirects=2 测试实际断言请求数为 3（`tests/test_safe_download.py:305-318`）。

问题：visited 不含初始 URL 导致自环多发一次；IPv6 display 丢括号使合法 IPv6 重定向坏掉；畸形 Location 可泄出 ValueError；缺 Location/循环在最后允许一跳会先归为 too_many_redirects。总时间只复用 deadline 数值，没有端到端强制截止，见 A1。其他 3xx 不会自动跟随，但有非空 body 时也可能被当成功，建议显式定义只接受哪些成功状态。

### Q7. aiter_bytes 计的是解码后字节吗？

对已识别且当前安装支持的 Content-Encoding（gzip/deflate 等）是。`httpx/_models.py:994-1005` 的顺序是 raw -> decoder.decode -> yield；`aiter_raw`（`:1037-1063`）输出尚未做内容解压的 body 字节，并非连 HTTP chunk framing 一起给出。未知/不支持的 Content-Encoding 不应被当成“已经正确解码”，应明确拒绝。

所以“超大的解码结果不会被成功返回”基本成立；“压缩炸弹不会突破内存/CPU预算”不成立，见 A2 的真实峰值。自动解码可以在第一次 yield 前完成巨量分配，外层异步 timeout 也不能抢占正在执行的同步解压。不能直接换成 `aiter_raw` 只按压缩后大小计数，那会丢掉现有的解码后限制。

### Q8. 错误分类有哪些，调用方分得开吗？

以下 reason 由 AST 从 DownloadError 构造点提取，行号均属 `src/image_search_mcp/safe_download.py`：

| reason | 行号 | 含义 |
|---|---|---|
| invalid_url | 167,172,179,182,188 | URL 入口显式拒绝 |
| dns_failure | 236,241 | resolver 异常/空结果 |
| blocked_address | 245 | 解析结果含非公网 |
| unsupported_proxy | 291 | 配置了下载代理 |
| timeout | 306,461 | 代码观察到总期限已到 |
| too_many_redirects | 317,353 | 跳数用尽 |
| redirect_without_location | 323 | 跳转缺 Location |
| https_downgrade | 332 | 禁止 HTTPS 降级 |
| redirect_loop | 338 | 已记录目标循环 |
| http_error | 344 | HTTP >=400，前提是已读完 body |
| empty_body | 349 | 非错误/非跳转响应为空 |
| connection_failed | 376 | 所有地址请求均抛 HTTPError/OSError |
| too_large | 427,469 | 声明长度或实际解码字节超限 |

调用方 `server.py:382-383` 将 reason 放进返回字符串，能看见类别，但没有按类别分支/重试，也不是结构化错误对象。普通异常走 `384-394` 记日志并返回内部错误；外部任务取消不被这里的 `except Exception` 吞掉。

确有归错/遮盖：ReadTimeout 和 DecodingError 变成 connection_failed；畸形 URL 变内部错误；503 的大/慢/坏 body 先变 too_large/timeout/connection_failed，盖过 HTTP 状态；DNS 无总期限会一直等。没有发现本应失败却静默返回成功的异常兜底。明确的静默项是无效 Content-Length 的 ValueError 被忽略（`:432-433`，仍按实际流量计数，所以不是大小绕过）以及首选 IP 失败后成功换地址、不报告先前失败（`:372-374`，这是设计上的重试）。

### Q9. install_cookie_jar 后域限定还在吗？

在。`credentials.py:267` 调用的真实 `httpx.Cookies.update` 先构造 Cookies，再逐 Cookie 对象 `set_cookie`（`.venv/lib/python3.11/site-packages/httpx/_models.py:1205-1208`）；Cookies 输入分支逐对象保留 jar（`:1094-1097`），不是转普通 dict。真实 Network `__aenter__` 返回 AsyncClient（`PicImageSearch/network.py:80-86`）。离线安装后检查得到 `[('yandex.com', '/', False)]`，元组依次为 domain/path/secure；本域与子域带上，其他域不带。测试也用真实对象通过，见 Q15。反转条件是依赖版本改变这个实现，或未来安装前主动将 jar 转 dict；本次版本未发生。

### Q10. Google 两个域会按浏览器语义互相收到 cookie 吗？

不会。`google.com` 与 `google.co.jp` 是独立域，互相不构成子域；这里没有公共后缀自动跨域共享的机制。真实 httpx 请求构造探针也确认单域 jar 不会跨过去。需要担心的恰恰是应用主动复制：`credentials.py:140-141` 对 Google 表的每个域分别 set 一份，所以当前配置两个域都会收到同一个值。这个行为需要明确授权/文档，见 B4。当前表没有直接把 `com`、`co.jp` 这类公共后缀当 cookie domain；不必为不存在的浏览器“自动跨域”加复杂 PSL 依赖，也不要假设 httpx 自动提供完整浏览器 PSL 安全策略。

### Q11. 后缀相同但不是子域会匹配吗？

当前实现不是自己写 `endswith`，而是委托 httpx/stdlib CookieJar。离线结果：

```text
yandex.com               sid=FIXTURE
a.yandex.com             sid=FIXTURE
evil-yandex.com           None
notyandex.com             None
yandex.com.evil.invalid   None
yandex.com.               None
```

因此点分隔边界是生效的，`evil-yandex.com` 不算子域。末尾带点的 FQDN 当前拿不到 cookie，是兼容性边缘；父域 jar 会给所有真正子域而非仅 API 子域，若有非受信任子域则范围仍过宽。建议加入上述显式边界用例，不应误报当前存在字符串后缀绕过。证据位置：`credentials.py:140-141,267`、`httpx/_models.py:1110-1141`。

### Q12. 11 个引擎是否遗漏第二方域名？

逐个追了真实请求调用，不只查 URL 字符串。下表 `P/` 代表 `.venv/lib/python3.11/site-packages/PicImageSearch/`。共同请求执行路径见 `P/engines/base.py:92-100`、`P/network.py:247-257,290-308`。所有 Network 默认跟随重定向（`P/network.py:65`），离线源码只能确认固定起点和动态调用点，不能证明线上最终跳转主机。

| 引擎 | 实际出站与证据 | 域名表结论 |
|---|---|---|
| Yandex | `P/engines/yandex.py:27-37,70-82`：yandex.com/images/search GET/POST；输入图 URL 作为参数 | 默认固定主机已覆盖，不本机下载输入图 |
| SauceNAO | `P/engines/saucenao.py:25,67-68,127-138`：saucenao.com/search.php POST | 已覆盖；返回的 Pixiv 等结果链接不是额外请求 |
| Ascii2D | `P/engines/ascii2d.py:33-45,86-104`：ascii2d.net 的 uri/file 上传；bovw 再对最终 resp.url 替换路径后 GET | 固定主机已覆盖，第二请求主机随响应/重定向而变，源码不能保证仍同域 |
| TraceMoe | `P/engines/tracemoe.py:60-78,119-123,208-228`：api.trace.moe/search 后逐项请求 trace.moe/anilist；me() 在 `98-100` 请求 api.trace.moe/me | 都属于 trace.moe，已覆盖。没有这条流程直接请求 graphql.anilist.co 的证据，不能仅看返回的番剧封面域就加 cookie |
| EHentai | `P/engines/ehentai.py:45-46,86-108`：upld.e-hentai.org/image_lookup.php；is_ex 分支为 upld.exhentai.org/upld/image_lookup.php | 两个父域覆盖。库原 URL 下载在 `89-92`，服务已绕过它，图源不应加入引擎 cookie 域 |
| Google | `P/engines/google.py:23-33,147-164`：默认 www.google.com/searchbyimage 与 upload；`104-108` 缺缩略图时重取结果页 resp.url；`55-60` 有显式分页请求 | google.com 覆盖默认主机。google.co.jp 仅在 `18` 行文档中作为 base_url 的示例，不能据字符串出现就说默认必访问 |
| GoogleLens | `P/engines/google_lens.py:32-33,96-129`：lens.google.com 上传/按 URL 搜索；指定非 all 类型后按页面链接访问默认 www.google.com | 固定域均被 google.com 覆盖，后续链接/自动重定向并不是出站白名单 |
| BaiDu | `P/engines/baidu.py:29-30,98-129`：graph.baidu.com/upload 后 GET 返回 JSON 的 data.url，再可能 GET 页面 tplData.firstUrl | 固定主机已覆盖，后两跳是动态 URL，无法仅凭源码穷尽实际域名；这是“只列字面量域名”最明显的盲点 |
| Bing | `P/engines/bing.py:31-32,48-54,78,94-118,151-161`：www.bing.com/images/search 上传及 images/api/custom/knowledge POST | 固定主机已覆盖。文件分支 `102-108` 清空 client cookies，独立于域名表正确与否 |
| Iqdb | `P/engines/iqdb.py:36-37,82-93`：iqdb.org 或 3d.iqdb.org POST，URL 为表单字段 | iqdb.org 覆盖两个主机；不本机抓输入图 |
| Tineye | `P/engines/tineye.py:24-31,160-181`：tineye.com/api/v1/result_json/；有 query hash 时追加 get_domains（`43`）；显式分页改写 resp.url（`63-70`） | 固定 API 都已覆盖；匹配网站域、图像结果只是数据，非逐个下载 |

辅助复核用 AST 打印了全部 11 个类的初始化默认值与 `self._send_request/self.download` 的调用行，确认上述调用确实存在。Google 的探针输出 `JP_TEXT [(18, 'Example: ...google.co.jp...')]`，没有把注释当成实际默认请求。

结论：没找到一个“源码固定请求但域名表漏写”的额外 CDN/头像域；也没把握断言“所有线上出站域都已列全”。BaiDu、Ascii2D、结果页/分页与通用自动重定向都有动态来源；扩大 cookie 域必须有真实请求和凭据需求证据，不能见一个图片 URL 就登记。域名表目前限制的是 cookie 分发，并不限制请求本身。上游构造函数允许改 base_url 的场景也不能套用“默认域已覆盖”的结论；本服务此次只调用 search，没有主动调用 Google/Tineye 分页（`server.py:436-446`）。

### Q13. 独立无凭据下载隔离是真的吗？

在当前可见的用户图源路径中是真的：`server.py:419-424` 先准备输入，之后才读取引擎凭据；EHentai/BaiDu 经 `356-359` 的新 downloader 下载后传 `file=bytes`，不会走上游 `self.download(url)`。SafeDownloader 每地址创建独立 AsyncClient，不接收引擎 client/headers/cookie（`safe_download.py:388-412`）；每个重定向 hop 又创建新 client，因此前一跳 Set-Cookie 也不会自动继承。其他引擎把输入 URL 交给搜索站点，不在本机下载输入图。

已补离线集成探针：给两个引擎各配置假 cookie，调用真实 `_prepare_search_input`，只替换 downloader 的 DNS 与 transport。实际结果：

```text
PREPARE EHentai {'file': b'IMG'}
PREPARE BaiDu {'file': b'IMG'}
DOWNLOAD_REQUESTS [('8.8.8.8', None, None), ('8.8.8.8', None, None)]
```

两个 None 分别是 Cookie 和 Authorization。没有发现带凭据 client 被复用去抓用户图源。但这不包含搜索引擎自己请求的结果页/动态接口，见 Q12；未来允许不受信任 transport_factory 也会突破此保证。换了未知上游版本，必须重跑本地抓图引擎集合契约。

### Q14. 还有“任何失败都算通过”的测试吗？

没有再找到下载路径的宽泛异常元组或 `pytest.raises(Exception)`；各 DownloadError 拒绝测试通常都核对 reason，正向返回测试也在。策略参数测试 `523-529` 只断言 ValueError，信息略弱，但并非“任何异常都行”；那个有限慢流测试 `400-437` 也确实限定 timeout 和耗时。

真正薄弱的是观测点和空断言，而非异常元组：

- 环境代理测试 `463-472` 在注入 transport 时天然绕开环境代理查询，改成 trust_env=True 仍通过，已做内存变异验证。
- 所谓“连接层实际地址”测试 `145-160` 只到自定义 transport 的 Request，不能验证真实 socket/TLS；本次 Q5 下沉到 httpcore backend 补了证据，但仍不伪称做过真实握手。
- `test_downloader_sends_no_cookies`（`482-486`）没有先准备任何引擎凭据，也不走 server 集成，不能独立发现 client 被错误复用。
- credentials 的源码域名测试 `46-60` 只验证一个方向，少写 google.co.jp 仍通过；注释也能满足它。它注释宣称能发现“少域/多域”，实际达不到。
- `test_unscoped_cookie_string_leaks_to_every_domain`（`236-247`）只遍历 recorder，缺少请求数量/目标断言；recorder 若意外为空可空循环通过，建议先断言两个实际目标均被记录。
- 下载超限测试验证了 reason，但 `334-373` 没断言 stream 关闭/停止消费的位置；“返回 too_large”并不证明早停、内存峰值受限。缺压缩回归、DNS deadline、响应头慢流、取消清理与上述地址边界。

修测试时应分别做小范围内存变异/有限坏样本，确认对应测试真的失败；不要重新引入只靠全局超时退出的无限流。

### Q15. credentials 测试用真实 Network 吗？会绕过丢域名的层吗？

用真实 Network（`tests/test_credentials.py:21,215-219,230-232,253-256`）。conftest 只包装真实 httpx 构造并注入 MockTransport（`tests/conftest.py:71-83`），Cookies 的构造、合并、请求头生成仍是真实现。因此它没有绕过这次关键的 jar 安装层，Q9 的结论可信。

但测试有两个明确边界：一是 transport 注入会掩盖默认环境代理路径；二是只验证安装和简单请求，不覆盖引擎内部后续变更。Bing 的离线探针实际输出：

```text
BING_BEFORE [('bing.com', 'sid')]
BING_AFTER [] REQUESTS [('www.bing.com', None)]
```

这里调用真实 `Bing._get_insights(bcid='FIXTURE')`，仅 HTTP 响应为 mock；这证明“已安装”不能推导“整个文件搜索始终带凭据”。是否应该改变 Bing 上游行为需要功能证据，目前归回归测试缺口，不宣称一定导致实际搜索失败。

## 4. 最薄弱的一环

资源预算只在外层检查，既没有完整包住 DNS/等待过程，也没有包住 httpx 的同步解压分配；“最终拒绝了响应”被误当成“消耗已经受控”。

## 5. 与任务书判断不同之处及反转条件

1. “远端下载上限 16 MiB”不符合当前 HEAD：实际 8 MiB，16 MiB 属于 Base64 入口（`safe_download.py:72`、`server.py:95,336`）。反转条件：调用方确实传入 16 MiB DownloadPolicy，或本次审阅的是另一明确 commit；当前版本没有。
2. “总期限覆盖 DNS、连接、重定向、读取全部阶段”不成立（A1）。反转条件：外围存在一个已核验的全程 deadline，连 DNS await/响应头等待/收尾都受它约束；本次可见入口没有，30 ms 的 DNS 探针要靠外层 150 ms wait_for 才能停止。
3. “aiter_bytes 计解码后字节”是对的；若从中推出“压缩炸弹不能突破资源预算”，这个推论错（A2）。反转条件：解码器本身有有界输出，或在读取前拒绝压缩响应；仅把最终 reason 保持 too_large、或改变 yield chunk_size，不足以反转。
4. “所有内网/特殊边界均拒绝、公网 IPv6 都正常”过宽：fec0::/10 放行；标准 NAT64 的公网转换地址也拒绝；IPv6 字面量 Host/重定向还有格式错误（A3、B1、Q4）。反转条件：把表述收窄到已经实测的地址与 URL 形态，或补齐分类和规范化修复，并在支持的 Python 版本跑相应边界正反例。
5. “cookie 表每项都来自引擎实际访问，源码字符串测试能核验齐全”不成立：google.co.jp 只有默认值示例意义；BaiDu 后续 URL 是动态响应数据；删掉一个域仍能通过现有门禁（B6、Q12）。反转条件：存在可复核的实际请求链或明确产品支持要求，并用能检出漏项的契约测试验证。不能用另一个注释或返回的 thumbnail 字段作证据。
6. 对问题 10 的担忧，重点应从“两个独立域是否自然共享”移到“应用是否有意复制”：CookieJar 不会自然互通，当前代码却主动复制了值（B4、Q10）。反转条件：需求明确授权同一引擎所有独立域共享同一串 cookie，此时它是有意配置语义，不是浏览器域匹配问题；需要在配置文档中写清。
7. `credentials.py` 所说“不接受隐式环境代理”作为端到端承诺不成立：load_proxy 忽略了，不代表 Network/httpx 忽略（B5）。反转条件：真实引擎 client 创建路径禁用 trust_env 且通过不被 MockTransport 掩盖的验证。生产尚未设置这些代理，不能因此推断代码已经实现承诺。

保留的判断：旧无域 cookie 的泄漏机制与本次修法方向正确；当前版本 `.update(jar)` 不丢域；默认 httpcore transport 的 IP pin/Host/SNI 连接链正确；逐跳地址校验与 HTTPS 降级拒绝主干有效；当前本机用户图源下载不复用带凭据 client。以上均不扩张成“任意依赖版本、任意自定义 transport、任意生产路由也安全”的结论。

交付边界：仅写这份报告；没有修实现、没有部署或检查远端服务。所有功能实验为离线探针与两份定向测试；内存变异只存在于短命 Python 子进程，未写回代码。生产仍运行旧代码且 cookie/proxy 路径未启用的信息来自任务书，本次不将新增模块的问题误写成已经发生的现网事故。

最终交付核验：独立新进程复跑相同定向测试，输出 `95 passed in 1.95s`；报告结构检查输出 `REVIEW_STRUCTURE_OK`，确认 Q1–Q15 完整且不重号、11 个引擎逐个覆盖、4 条“应当立刻修”与 6 条“值得做”。`git diff --exit-code` 与 `git diff --cached --exit-code` 均为 0；HEAD 仍是 `9e6cb4d301af2035e012124a2fa04021173c44e5`。最终 `git status --short` 仅显示本报告和任务开始前已有的 `docs/impl-review-A-brief.md` 两个未跟踪文件。


