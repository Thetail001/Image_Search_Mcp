# 代码复核任务书（A 部分）：两个新增模块的实现质量

## 你的角色

你在做一次**常规的代码质量复核**——一个自托管的 MCP 服务（Python，把反向图搜库
`PicImageSearch` 包成 MCP 工具，11 个搜索引擎）。目标是提升**正确性与健壮性**。

**这次只看两个文件**，其他都不要碰（另有两次复核会覆盖剩下的模块）。

### 硬性约束

- 仓库：`/root/hermes-workspace/image-search-mcp`（你就在这个目录里）
- **除了下面这一个输出文件，禁止创建、修改、删除仓库内任何其它文件。**
  需要做实验就写 `/tmp/`。
- **输出文件**：`docs/impl-review-A-astra.md`（这个允许你写，而且是**必须**写）。
- **边查边写，不要攒到最后。** 每完成一小块结论就用写文件工具落盘一次（追加）。
  上一次派给你同类任务时，你在侦察阶段跑了 28 分钟被超时中断，结论一个字都没留下。
  这次宁可先写下"尚未验证的初步判断"，也不要把它留在脑子里。
- **不要去抓外部网页**（规范、登记表、博客）。只读仓库里的代码和测试，必要时跑仓库自带的脚本。
  上一次光是抓外部资料就吃掉大半预算。
- **不要跑全量 `./check.sh`**（含反向测试，太慢）。要跑就单跑相关测试文件。
- 引用结论必须带 `文件:行号`，或者贴你实际跑出来的命令与输出。
- 按两档给优先级：**应当立刻修** 与 **值得做**。不要给第三档。
- 认为我哪个判断不对，直接说，并说明**什么条件下你会反转**。

---

## 生产实况（判正确性时缺这些就是空谈）

- 部署在一台美国 VPS 上，**目前跑的还是旧代码**，这批改动尚未上线。
- systemd 单元 `image-search-mcp.service`；`start.sh` → `uvx`；以 root 运行。
- 环境变量：`MCP_AUTH_TOKEN` 有值（长度 64）；`IMAGE_SEARCH_API_KEY` 有值；
  `IMAGE_SEARCH_COOKIES` **空**；`IMAGE_SEARCH_PROXY` **空**；`HTTP_PROXY`/`HTTPS_PROXY` **未设置**。
- 因为那两个变量是空的，旧代码里"非空即出错"的两条路径在线上一直没被走到
  （`Network` 收到 dict 而非 str）。
- 调用方是同一台机器上的其它服务（本机回环），不是公网直连。
- 打包没有版本锁定：`uvx` 从 `~/.cache/uv/archive-v0/RZS0poX6IzvIELO8/bin/` 取包，
  机器上有 9 份字节相同的重复包副本。`uv tool list` 是空的。

---

## 范围

复核 `09dda2e..HEAD`（HEAD = `9e6cb4d`）里的**这两个文件**：

| 文件 | 行数 | 说明 |
|---|---|---|
| `src/image_search_mcp/safe_download.py` | 481 | 新增：远端取图助手 |
| `src/image_search_mcp/credentials.py` | 267 | 新增：按引擎归属的 cookie 与出站域名 |

配套测试：`tests/test_safe_download.py`（490 行）、`tests/test_credentials.py`（268 行）。
也可以读 `tests/conftest.py`（125 行）了解测试环境怎么隔离网络。

---

## 我已经实测过的结论（不要复述，请复核或推翻）

**关于 cookie 归属。** 旧代码用 `httpx.AsyncClient(cookies={"sid": ...})`，产生的 cookie
**没有绑定域名**，会跟着请求发往任何域名。实测：这个 cookie 同时出现在发往 `untrusted.invalid`
和 `api.trace.moe` 的请求上；对照组用 `httpx.Cookies()` 绑定 `domain="yandex.com"`，
发往 `yandex.com` 时带上、发往别处时是 `Cookie: None`。
现在的修法是：按引擎构造绑定了域名的 jar，装到 `Network` 交出的真实 client 上
（`Network` 本身接不了 `Cookies` 对象）。

**关于取图助手的设计。** 要点是：地址分类检查（内网、回环、链路本地、保留网段一律不取）、
**每一跳重定向都重新检查**、把域名解析结果与最终连接地址绑定（用 `httpcore` 的
`extensions={"sni_hostname": ...}` 去连解析出的固定 IP，同时保留 SNI 与证书校验）、
体积与总期限预算、`trust_env=False`。
实网探针：连上了固定的 IP，`Host` 与 `sni_hostname` 都还是原域名，证书校验通过；
内网目标都被拒绝；公网 IPv6 正常放行。

---

## 请核对的问题

### 一、`safe_download.py`

1. **它不检查 `Content-Type`。** 任何内容都会被当成图片字节交给引擎。这算问题吗？
2. **整个响应体在内存里累积**（上限 16 MiB）。够吗？并发 50 个调用会怎样？
3. **没有并发上限。** 整条链路（包括调用方 `server.py`）我找不到任何并发限制。这要不要管？
4. `is_public_address()` 的地址分类边界：IPv4-mapped IPv6、6to4、Teredo、NAT64（`64:ff9b::/96`）、
   运营商级 NAT（`100.64/10`）、`0.0.0.0/8`、广播地址，以及各类保留网段的特例。
   **我覆盖了哪些、漏了哪些？请逐段核对，不要只看我列出来的这几个。**
5. 解析结果与连接之间的一致性：我按解析结果去连那个 IP，所以"解析之后地址被换掉"应该不会发生
   —— 这个结论成立吗？如果 `transport_factory` 被换成别的实现呢？
6. 重定向规则：我拒绝 https→http 的降级，并且（大概）限制了跳数。**这条路径的完整性请你查。**
7. `_read_bounded` 用 `aiter_bytes()`，我声称计的是**解码后**的字节数，因此"压缩后很小、解压后
   很大"的响应不会绕过预算。**这个声称对不对？`aiter_bytes` 与 `aiter_raw` 的区别是什么？**
8. 错误分类：它把失败分成哪些 `reason`？调用方（`server.py`）区分得开吗？有没有哪些失败
   会被静默吞掉或归错类？

### 二、`credentials.py`

9. `install_cookie_jar()` 用 `client.cookies.update(jar)` 把绑定了域名的 jar 装到 `Network` 交出的
   真实 client 上。**装上去之后域名限定还在吗？** httpx 内部会不会在这一步把 `Cookies` 拆成
   普通 dict 而丢掉域名？（注意：旧代码正是在"跨这一层"的时候把类型弄错了。）
10. **公共后缀的问题**：`google.com` 与 `google.co.jp` 都登记为 Google 的域名。给 `google.co.jp`
    设的 cookie，按浏览器语义会不会也被发往 `google.com`？反过来呢？我需要担心吗？
11. 子域匹配的边界：我（大概）是按"域名后缀"匹配的。域名结尾相同但并不构成子域的情况怎么处理的？
    会不会把 `evil-yandex.com` 也算成 `yandex.com` 的子域？**请指出这种边界情况。**
12. 域名表里我只写了引擎源码里出现过的域名。**有没有哪个引擎其实还会访问没登记的第二方域名？**
    比如 CDN、`api.` 子域、头像服务。请对着 `PicImageSearch`（装在 `.venv` 里）的源码逐个引擎核对。
13. 无凭据的下载路径：我让下载走一个**不带凭据的独立 client**。这个隔离是真的吗？
    有没有哪条路径会让带凭据的 client 被复用去下载？

### 三、这两份测试

14. `tests/test_safe_download.py` 里的断言，有没有**同义反复**的？（我修过两条：
    一条用 `pytest.raises((A, B, C))` 这种宽泛异常元组；一条用 `while True` 造响应，只能靠超时结束。
    还有没有别的"只要因为任何原因失败就算通过"的写法？）
15. `tests/test_credentials.py` 用的是真实 `Network` 对象还是替身？如果是替身，
    它会不会刚好绕过了真实实现里那个会丢域名的地方？

---

## 你可以怎么验

```bash
cd /root/hermes-workspace/image-search-mcp
.venv/bin/python -m pytest -q tests/test_safe_download.py tests/test_credentials.py
.venv/bin/python -c "..."        # 自己写探针
```

注意：测试环境默认**禁网**（`httpx` 被注入拒绝一切的 transport，且 `socket.getaddrinfo` 被拦）。
自己起脚本要用 `.venv/bin/python` 直接跑（不走 pytest 夹具），或在自己的脚本里显式放开。

---

## 输出要求

写进 `docs/impl-review-A-astra.md`，结构：

1. **应当立刻修**（每条：`文件:行号` + 为什么 + 你怎么验的 + 建议改法）
2. **值得做**（同上）
3. 对上面 **15 个问题逐个**直接回答。没把握就说"没把握"——那比略过有用
4. 一句话：**这两个文件里最薄弱的一环是什么**
5. 如果你认为我哪条结论错了，单独列出来

不要写客套话。**记得边写边落盘。**
