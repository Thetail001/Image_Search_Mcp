# Image Search MCP Server

一个基于 [PicImageSearch](https://github.com/kitUIN/PicImageSearch) 的以图搜图 MCP 服务器。支持多种搜索引擎（SauceNAO, Google, TraceMoe, etc.）。

## 功能

- 支持 URL 或 Base64 图片搜索
- 支持多种搜索引擎切换
- 支持代理设置
- 支持 API Key 配置（如 SauceNAO）

## 依赖

- Python 3.10+ (推荐 3.10 - 3.12，Python 3.13+ 可能因 `lxml` 兼容性问题导致安装失败)
- `mcp` (FastMCP)
- `PicImageSearch`

## 安装

1. 克隆项目或进入目录。
2. 创建虚拟环境（可选但推荐）：
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Linux/Mac
   .venv\Scripts\activate     # Windows
   ```
3. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```

## 使用

### 运行服务器

```bash
# 开发模式
mcp dev src/server.py

# 或者直接运行
python src/server.py
```

### 工具列表

#### `search_image`
执行以图搜图。

**参数：**
- `engine` (str): 搜索引擎名称。支持：`SauceNAO`, `Google`, `TraceMoe`, `Ascii2D`, `EHentai`, `Iqdb`, `Tineye`, `Yandex` 等。
- `source` (str): 图片的 URL 或 Base64 编码字符串。
- `api_key` (str, 可选): 搜索引擎的 API Key (例如 SauceNAO 需要)。
- `proxy` (str, 可选): 代理地址 (例如 `http://127.0.0.1:7890`)。
- `extra_params_json` (str, 可选): JSON 格式的额外参数 (例如 `{"numres": 5, "hide": 0}`)。

#### `list_engines`
列出所有支持的搜索引擎。

## 常见问题

**Q: 安装 `lxml` 失败？**
A: 请确保您使用的 Python 版本兼容 `lxml`（推荐 Python 3.12）。如果是 Windows 且使用最新 Python，可能需要安装 Microsoft C++ Build Tools。
