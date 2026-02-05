# Image Search MCP Server

一个基于 [PicImageSearch](https://github.com/kitUIN/PicImageSearch) 的 MCP 服务器，支持多引擎以图搜图。

支持通过 `uvx` (uv) 或 `pipx` 一键运行，无需手动克隆代码。

## ✨ 特性

- **多引擎支持**：集成 11 种主流搜图引擎 (Yandex, SauceNAO, Google, TraceMoe, ASCII2D, EHentai, Iqdb, BaiDu, Bing, GoogleLens, Tineye)。
- **结果精简**：自动移除原始冗余数据，仅返回最关键的标题、链接、缩略图等信息。
- **智能提示**：当因机器人验证（Bot Protection）导致无结果时，自动提示配置 Cookie。
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

#### 🔒 开启安全认证 (可选)

如果在公网部署，建议设置环境变量来开启 Bearer Token 认证。

**全量环境变量配置示例：**

**Linux/macOS**:
```bash
export MCP_AUTH_TOKEN="my-secret-token-123"
export IMAGE_SEARCH_API_KEY="your_saucenao_key"
export IMAGE_SEARCH_COOKIES="your_cookies_here"
export IMAGE_SEARCH_PROXY="http://127.0.0.1:7890"

uvx image-search-mcp --sse --host 0.0.0.0 --port 8000
```

**Windows (PowerShell)**:
```powershell
$env:MCP_AUTH_TOKEN="my-secret-token-123"
$env:IMAGE_SEARCH_API_KEY="your_saucenao_key"
$env:IMAGE_SEARCH_COOKIES="your_cookies_here"
$env:IMAGE_SEARCH_PROXY="http://127.0.0.1:7890"

uvx image-search-mcp --sse --host 0.0.0.0 --port 8000
```

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
| `MCP_AUTH_TOKEN` | SSE 模式下的 Bearer Token 认证 | `my-secret-token-123` |
| `IMAGE_SEARCH_API_KEY` | SauceNAO API Key (用于 SauceNAO 引擎) | `your_api_key` |
| `IMAGE_SEARCH_COOKIES` | 通用 Cookies (用于 Google, Bing, Tineye, EHentai 等) | `igneous=...; ipb_member_id=...` |
| `IMAGE_SEARCH_PROXY` | HTTP 代理地址 (优先级最高) | `http://127.0.0.1:7890` |
| `HTTP_PROXY` / `HTTPS_PROXY` | 通用系统代理 (备选) | `http://127.0.0.1:7890` |

### 关于 Cookies 的重要提示
Google, Bing, GoogleLens 和 Tineye 等引擎经常会有机器人验证（CAPTCHA）。如果搜索返回 "No results found" 或提示 Bot Protection，请尝试在浏览器中访问对应搜索引擎，登录并获取 Cookies，然后设置到 `IMAGE_SEARCH_COOKIES` 环境变量中。

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
        "IMAGE_SEARCH_COOKIES": "your_cookies",
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
| `source` | string | 是 | - | 图片 URL (`http://...`) 或 Base64 字符串。 |
| `engine` | string | 否 | **"Yandex"** | 搜索引擎名称。支持: Yandex, SauceNAO, Google, TraceMoe, Ascii2D, EHentai, Iqdb, BaiDu, Bing, GoogleLens, Tineye。 |
| `extra_params_json` | string | 否 | - | JSON 字符串，用于传递引擎特定的高级参数。 |
| `limit` | int | 否 | 5 | 返回结果的最大数量。 |

**调用示例:**

```json
{
  "engine": "Ascii2D",
  "source": "https://example.com/image.jpg",
  "extra_params_json": "{\"bovw\": true}",
  "limit": 3
}
```

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

- Python >= 3.10
- *注意*：依赖库 `lxml` 建议使用 Python 3.12。
