# Image Search MCP Server

一个基于 [PicImageSearch](https://github.com/kitUIN/PicImageSearch) 的 MCP 服务器，支持多引擎以图搜图。

支持通过 `uvx` (uv) 或 `pipx` 一键运行，无需手动克隆代码。

## ✨ 特性

- **多引擎支持**：集成 11 种主流搜图引擎 (Yandex, SauceNAO, Google, TraceMoe, ASCII2D, EHentai, Iqdb, BaiDu, Bing, GoogleLens, Tineye)。
- **结果精简**：自动移除原始冗余数据，仅返回最关键的标题、链接、缩略图等信息。
- **空结果提示**：没匹配到结果时给出**排查顺序**（图本身、覆盖范围、限流、是否需要凭据），不替上游断定"是机器人验证"。
- **零配置部署**：通过 `uvx` 直接运行。
- **安全配置**：API Key 和代理设置通过环境变量管理。
- **灵活输入**：支持图片 URL 和 Base64 编码。

## 🚀 快速开始

### 方式 1: 直接运行 (Stdio 模式)

适用于 Claude Desktop 或其他支持 MCP Stdio 的客户端。

**Command**:
```bash
uvx image-search-mcp
```

### 方式 2: SSE 模式 (HTTP Server)

适用于远程部署或 Web 客户端。

```bash
uvx image-search-mcp --sse --port 8000
```

#### 🔒 安全认证

SSE/HTTP 模式下 **`MCP_AUTH_TOKEN` 是必需的** —— 缺失或只有空白时服务拒绝启动并
以非零码退出（旧版本会照常起一个任何人都能调的服务，那是认证绕过的前提）。

**环境变量示例：**

**Linux/macOS**:
```bash
export MCP_AUTH_TOKEN="$(openssl rand -hex 32)"     # 必需
export IMAGE_SEARCH_API_KEY="your_saucenao_key"     # 可选（SauceNAO）
export IMAGE_SEARCH_COOKIES_YANDEX="your_cookies"   # 可选，按引擎命名
export IMAGE_SEARCH_PROXY="http://127.0.0.1:7890"   # 可选

uvx image-search-mcp --sse --host 0.0.0.0 --port 8000
```

**Windows (PowerShell)**:
```powershell
$env:MCP_AUTH_TOKEN="my-secret-token-123"
$env:IMAGE_SEARCH_API_KEY="your_saucenao_key"
$env:IMAGE_SEARCH_COOKIES_YANDEX="your_cookies"
$env:IMAGE_SEARCH_PROXY="http://127.0.0.1:7890"

uvx image-search-mcp --sse --host 0.0.0.0 --port 8000
```

**认证面（收窄后的）**

- `/healthz` 的 `GET` / `HEAD` 免认证。**它不访问任何引擎** ——
  真实搜图会烧配额，还会让上游抽风传导成本机重启
- 其余所有路径（含 `/`）都需要 `Authorization: Bearer <token>`
- 带 `Origin` 头的请求默认被拒（防 DNS rebinding）。浏览器类客户端需要把来源
  写进 `IMAGE_SEARCH_ALLOWED_ORIGINS`（逗号分隔）；原生 MCP 客户端不发这个头，不受影响
- WebSocket 一律拒绝（握手阶段回 1008）
- 仅本机调试可开匿名：`IMAGE_SEARCH_ALLOW_ANONYMOUS=1` **且** `--host 127.0.0.1`

> 这一批修复改变了运行期行为。升级前请读 [`docs/配置变更说明.md`](docs/配置变更说明.md)。

#### 客户端连接示例 (SSE)

**JSON 配置示例 (例如用于 Claude Desktop 或其他支持远程 MCP 的客户端):**

```json
{
  "mcpServers": {
    "image-search-remote": {
      "type": "sse",
      "url": "http://your-server-ip:8000/sse",
      "headers": {
        "Authorization": "Bearer my-secret-token-123"
      }
    }
  }
}
```

---

## ⚙️ 环境配置说明

以下是所有支持的环境变量：

| 环境变量 | 说明 | 示例 |
| :--- | :--- | :--- |
| `MCP_AUTH_TOKEN` | **HTTP 模式必需**。缺失或空白则拒绝启动 | `$(openssl rand -hex 32)` |
| `IMAGE_SEARCH_API_KEY` | SauceNAO API Key。**只从环境读**，不接受调用方传入 | `your_api_key` |
| `IMAGE_SEARCH_COOKIES_<引擎>` | 按引擎命名的 Cookies，例如 `IMAGE_SEARCH_COOKIES_YANDEX`、`IMAGE_SEARCH_COOKIES_EHENTAI`、`IMAGE_SEARCH_COOKIES_GOOGLE` | `igneous=...; ipb_member_id=...` |
| `IMAGE_SEARCH_PROXY` | HTTP 代理地址 | `http://127.0.0.1:7890` |
| `IMAGE_SEARCH_ALLOWED_ORIGINS` | 允许的浏览器 `Origin`（逗号分隔）。不设置时任何带 `Origin` 的请求都被拒 | `https://a.example,https://b.example` |
| `IMAGE_SEARCH_ALLOW_ANONYMOUS` | 仅本机调试：`1` 且绑定回环地址时可不认证 | `1` |
| `HTTP_PROXY` / `HTTPS_PROXY` | 系统代理。**引擎自身的请求**遵循 httpx 默认（会读）；取图下载器刻意不读（`trust_env=False`） | `http://127.0.0.1:7890` |

> **`IMAGE_SEARCH_COOKIES`（无后缀的全局变量）不再被接受。**
> 一个不带域名的 cookie 会跟着请求发给**任何**域名 —— 实测把 `sid=...` 发到了
> `untrusted.invalid`。检测到全局变量时服务会拒绝这次调用，并指出该用哪个变量名。
> 归属不明时宁可拒绝，也不猜一个引擎发出去（猜错就是把 A 站凭据发给 B 站）。

### 关于 Cookies 的重要提示

有些引擎（Yandex、Google、Bing、GoogleLens、Tineye 等）确实可能要求凭据，但
**"No results found." 不等于机器人验证** —— 服务也不会这样断言（旧文案这么写过，是猜的）。
没结果时按这个顺序排查：

1. **先看有没有明确失败信息**。上游明确报错时会返回 `Error: …`（`isError=true`），
   那是可判定的事实，照它修；只有 "No results found." 才是"搜索成功、上游没报错、就是没匹配"。
2. **图本身**：太小、太模糊、被裁过，任何引擎都匹配不到。
3. **覆盖范围**：收录这张图的站点不在该引擎的索引里。
4. **限流 / 需要凭据**：到这一步再考虑配 cookies —— 在浏览器里登录对应搜索引擎，
   拿到 cookies 后设到**对应引擎的** `IMAGE_SEARCH_COOKIES_<引擎>`（例如 `IMAGE_SEARCH_COOKIES_YANDEX`）。

配 cookie **不保证**能绕过风控。Bing 尤其要注意：它在本项目里标为 `experimental`，
原因是上游 PyPI 3.12.11 **缺少签名解密修复**（该修复尚未发版），空结果可能与之有关 ——
这种情况配 cookie 也不会改善。

每个引擎的 cookie 只会发给它自己真正访问的域名（Yandex → `yandex.com`，EHentai → `e-hentai.org` / `exhentai.org`，依此类推）。这张域名表对着上游源码核验过，并有测试盯着。

---

## 💻 客户端部署指南 (本地 Stdio)

### Claude Desktop 配置 (Stdio)

编辑 `claude_desktop_config.json`，在 `env` 字段中配置本地运行所需的变量：

```json
{
  "mcpServers": {
    "image-search-local": {
      "command": "uvx",
      "args": ["image-search-mcp"],
      "env": {
        "IMAGE_SEARCH_API_KEY": "your_saucenao_key",
        "IMAGE_SEARCH_COOKIES_YANDEX": "your_cookies",
        "IMAGE_SEARCH_PROXY": "http://127.0.0.1:7890"
      }
    }
  }
}
```

---

## 🛠 工具使用指南

### `search_image`

**参数列表：**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| `source` | string | 是 | - | 图片 URL (`http://...`) 或 Base64 字符串。URL 必须以 `http://` / `https://` 开头；Base64 输入上限 16 MiB。 |
| `engine` | string | 否 | **"Yandex"** | 搜索引擎名称。支持: Yandex, SauceNAO, Google, TraceMoe, Ascii2D, EHentai, Iqdb, BaiDu, Bing, GoogleLens, Tineye。 |
| `extra_params_json` | string | 否 | - | JSON 对象字符串，用于传递**该引擎真实支持**的参数。见下方说明。 |
| `limit` | int | 否 | 5 | 返回结果的最大数量，必须在 **1–50** 之间。越界或非整数会在发出任何请求之前被拒。 |

**关于 `extra_params_json`：只接受该引擎真实支持的参数。**

白名单来自对已安装 PicImageSearch **真实函数签名**的检查（不是从文档或旧配置手抄），
由 `tests/test_param_contract.py` 逐项核验。三类输入会被拒绝：

1. **未知参数** —— 报错并列出允许的名字。例如 SauceNAO 的 `output_type` 虽然存在于上游
   签名里，但本库的解析固定走 JSON，设成别的值会让解析失败，因此**刻意不开**
2. **保留键** —— `url` / `file` / `client` / `api_key` / `cookies` / `proxies` /
   `request_kwargs` / `base_url` 一律拒绝。早期版本允许覆盖它们，
   于是 `{"file": "/etc/passwd"}` 能把输入换成本地路径，读出来的内容会被**上传到搜索引擎站点**
3. **顶层不是对象** —— 数组、标量都拒绝

用 `get_engine_info` 查看某个引擎的完整参数列表。

**调用示例（有效参数）：**

```json
{
  "engine": "TraceMoe",
  "source": "https://example.com/image.jpg",
  "extra_params_json": "{\"cut_borders\": false}",
  "limit": 3
}
```

```json
{
  "engine": "Ascii2D",
  "source": "https://example.com/image.jpg",
  "extra_params_json": "{\"bovw\": true}"
}
```
（`bovw` 是 Ascii2D 的初始化参数：`true` = 特征搜索，对裁剪/改图更有效）

> 参数名用**上游的真实拼写**。旧文档里那个 `cutBorders`（驼峰）从来没生效过 ——
> 它会落进上游的 `**kwargs` 被静静吞掉。现在这类拼错会被明确拒绝并指出正确写法。

### `get_engine_info`

获取支持的搜索引擎列表或特定引擎的详细参数信息。

**调用示例:**
```json
{
  "engine_name": "all" 
}
```
或
```json
{
  "engine_name": "SauceNAO"
}
```

---

## 📦 开发与发布

### 测试与验收

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"

./check.sh                 # 一键验收：单元测试 + 门禁自检 + 反向测试
./check.sh --fast          # 跳过反向测试
./check.sh --reverse-only  # 只跑反向测试
```

测试的默认姿态是**禁网**：`tests/conftest.py` 会给 `httpx` 注入"拒绝一切"的 transport，
并拦掉真实 DNS 解析。任何未被显式 mock 的出站请求都会立刻失败，而不是真的打出去。

`tools/reverse_tests/` 是**反向测试**：把已经修好的缺陷重新注入到代码副本里，
要求指定测试因此变红。没有它，一片绿的测试无法区分"代码是对的"和"测试没在看"。
每条注入都写明对应哪个原始缺陷，执行器还会先确认导入到的是副本里的模块 ——
否则跑的仍是干净代码，反向测试是假的。

| 工具 | 用途 |
| :--- | :--- |
| `tools/reverse_tests/run.py` | 反向测试（`--list` 看注入清单） |
| `tools/gate_tests_not_ignored.py` | 门禁：测试源码没被 `.gitignore` 吞掉（`--self-test` 自检） |
| `tools/status.py --update docs/实施进度.md` | 重新生成状态段，避免手抄数字 |

### 构建与上传

1.  **安装构建工具**:
    ```bash
    pip install build twine
    ```

2.  **构建**:
    ```bash
    python -m build
    ```

3.  **上传到 PyPI**:
    ```bash
    twine upload dist/*
    ```

## 环境要求

- Python >= 3.10（CI 矩阵覆盖 3.10 / 3.11 / 3.12）
- *注意*：依赖库 `lxml` 建议使用 Python 3.12。

## 依赖策略

**不跟 PicImageSearch 的 `main` 分支。** PyPI 上的 `3.12.11` 就是本项目使用的版本，
而 `main` 领先 78 个提交且尚未发布，其中包含 httpx→httpx2 这种破坏性迁移。
上游 `yandex.py` 在这期间只改了类型标注 —— 我们最依赖的那个引擎没有行为变化。

"上游改了签名没人知道"这件事由三个机制兜住，而不是靠给依赖加上界：

1. `tests/test_param_contract.py` 对着 `inspect.signature` 逐项比对参数契约
2. CI 里跑一遍完整测试
3. 低频的在线抽验

`pyproject.toml` 里刻意不给依赖上界：上界解决不了"上游改了行为"，
只会让人以为锁住了。
