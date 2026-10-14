# Image Search MCP

基于 [PicImageSearch](https://github.com/kitUIN/PicImageSearch) 的以图搜图 MCP 服务。支持图片 URL、Base64 和 Base64 data URI，提供 stdio 与 SSE 两种连接方式。

搜索结果包含标题、来源链接、缩略图和相似度；部分引擎还会返回作者、番剧集数或时间点。结果同时提供文本和 `structuredContent`。

## 快速开始

需要 Python 3.10 或更高版本，以及 [uv](https://docs.astral.sh/uv/) 或 pipx。

### 本地运行（stdio）

```bash
uvx image-search-mcp
```

也可以使用 `pipx run image-search-mcp`。stdio 模式由 MCP 客户端启动，不需要 HTTP 认证令牌。

以下是支持 stdio 的客户端配置示例。SauceNAO API Key 和代理均为可选项，用不到时删除对应变量即可。

```json
{
  "mcpServers": {
    "image-search": {
      "command": "uvx",
      "args": ["image-search-mcp"],
      "env": {
        "IMAGE_SEARCH_API_KEY": "your_saucenao_key",
        "IMAGE_SEARCH_PROXY": "http://127.0.0.1:7890"
      }
    }
  }
}
```

### HTTP 服务（SSE）

HTTP 模式必须设置 `MCP_AUTH_TOKEN`。下面的命令默认监听 `127.0.0.1:8000`：

```bash
export MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
uvx image-search-mcp --sse --port 8000
```

Windows PowerShell：

```powershell
$env:MCP_AUTH_TOKEN = "replace-with-a-long-random-token"
uvx image-search-mcp --sse --port 8000
```

需要其他机器访问时添加 `--host 0.0.0.0`，并在反向代理上配置 HTTPS。请求头应携带 `Authorization: Bearer <token>`。

SSE 地址为 `/sse`，消息端点为 `/messages/`。远程 MCP 配置格式取决于客户端；以下示例适用于接受 `type`、`url` 和 `headers` 字段的客户端：

```json
{
  "mcpServers": {
    "image-search-remote": {
      "type": "sse",
      "url": "https://your-server.example/sse",
      "headers": {
        "Authorization": "Bearer replace-with-your-token"
      }
    }
  }
}
```

`GET /healthz` 和 `HEAD /healthz` 无需认证，只检查服务是否运行，不访问搜图引擎。其他路径均要求认证。

带 `Origin` 请求头的请求还需匹配 `IMAGE_SEARCH_ALLOWED_ORIGINS`。未配置时会拒绝此类请求；不发送 `Origin` 的原生客户端不受影响。WebSocket 不受支持。

本机调试可使用 `--allow-anonymous`，或设置 `IMAGE_SEARCH_ALLOW_ANONYMOUS=1`；匿名模式只能监听回环地址。

## 搜索引擎

| 名称 | 用途与说明 |
| --- | --- |
| `Yandex` | 通用图片来源与相似图片搜索，默认引擎 |
| `SauceNAO` | 插画、漫画与动漫图片来源，可配置 API Key |
| `TraceMoe` | 根据动漫截图查询番剧、集数和时间点 |
| `Ascii2D` | 插画来源查询，支持颜色与特征搜索 |
| `EHentai` | E-Hentai / ExHentai 图库搜索；ExHentai 需要 Cookies |
| `Iqdb` | 多个插画站点的相似图片搜索 |
| `BaiDu` | 百度识图 |
| `GoogleLens` | Google Lens 图片搜索 |
| `Tineye` | 图片来源与重复图片搜索 |
| `Google` | 已标记弃用，建议使用 `GoogleLens` |
| `Bing` | 实验性；当前支持的上游版本可能返回空结果 |

引擎是否能返回结果取决于图片、索引覆盖、网络和上游限制。用 `get_engine_info` 查看各引擎的参数与支持状态。

## 配置

所有凭据均在服务端通过环境变量配置。

| 环境变量 | 说明 |
| --- | --- |
| `MCP_AUTH_TOKEN` | HTTP 模式的 Bearer 令牌；缺失或仅含空白时拒绝启动 |
| `IMAGE_SEARCH_API_KEY` | SauceNAO API Key |
| `IMAGE_SEARCH_COOKIES_<ENGINE>` | 指定引擎的 Cookies，后缀使用大写引擎名，如 `YANDEX`、`EHENTAI` |
| `IMAGE_SEARCH_PROXY` | 搜索引擎请求使用的代理，如 `http://127.0.0.1:7890` |
| `IMAGE_SEARCH_DEFAULT_ENGINE` | 未指定 `engine` 时使用的引擎，默认 `Yandex`；可设为 `All` |
| `IMAGE_SEARCH_ALLOWED_ORIGINS` | 允许的浏览器来源，逗号分隔，如 `https://client.example` |
| `IMAGE_SEARCH_ALLOW_ANONYMOUS` | 设为 `1` 时开启仅限回环地址的匿名 HTTP 调试模式 |

Cookies 示例：

```bash
export IMAGE_SEARCH_COOKIES_YANDEX="sid=...; other=..."
export IMAGE_SEARCH_COOKIES_EHENTAI="igneous=...; ipb_member_id=..."
```

Cookies 只发送给对应引擎的域名，并且仅通过 HTTPS 发送。旧的全局 `IMAGE_SEARCH_COOKIES` 只有同时设置 `IMAGE_SEARCH_COOKIES_ENGINE` 指明归属时才兼容；新配置请使用引擎专属变量。

`HTTP_PROXY`、`HTTPS_PROXY` 等通用代理变量不会用于引擎请求。本机图片下载也不使用代理或引擎凭据；`IMAGE_SEARCH_PROXY` 仅影响向搜索引擎发出的请求。

升级旧版本时，请查看 [配置变更说明](docs/配置变更说明.md)。

## MCP 工具

### `search_image`

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `source` | string | 必填 | `http://` / `https://` 图片 URL、Base64 或 `data:image/...;base64,...` |
| `engine` | string 或 null | 环境配置或 `Yandex` | 名称不区分大小写；`All` 查询全部引擎；省略或 null 时使用默认引擎 |
| `extra_params_json` | string 或 null | null | 引擎参数的 JSON 字符串；省略、空字符串和 JSON `null` 均表示不传参数 |
| `limit` | integer | 5 | 返回条数，范围为 1–50 |

Base64 解码后上限为 16 MiB。不接受本地文件路径。

`extra_params_json` 只接受该引擎支持的参数。未知参数、错误类型和由服务端管理的键（如 `file`、`url`、`api_key`、`cookies`、`proxies`）会被拒绝。参数列表由 `get_engine_info` 提供。

TraceMoe 示例：

```json
{
  "engine": "TraceMoe",
  "source": "https://example.com/image.jpg",
  "extra_params_json": "{\"cut_borders\": false}",
  "limit": 3
}
```

Ascii2D 特征搜索示例：

```json
{
  "engine": "Ascii2D",
  "source": "https://example.com/image.jpg",
  "extra_params_json": "{\"bovw\": true}"
}
```

All 查询示例：

```json
{
  "engine": "All",
  "source": "https://example.com/image.jpg",
  "extra_params_json": "{\"bovw\": true, \"cut_borders\": false}",
  "limit": 3
}
```

All 模式的 `limit` 按每个引擎计算。附加参数只传给支持该参数的引擎，例如 `bovw` 传给 Ascii2D，`cut_borders` 传给 TraceMoe；未知参数、保留键和错误类型会在查询开始前被拒绝。

每个引擎使用独立客户端与自身凭据。每次 All 查询最多并发 3 个引擎，单个引擎限时 20 秒，整次查询限时 60 秒；总期限到达时保留已完成的结果，并取消剩余任务。单引擎查询同样有 60 秒总期限。

EHentai 和 BaiDu 的 URL 输入由本机下载，再以图片字节提交给引擎。下载限制为 8 MiB、15 秒、最多 3 次重定向；非公网地址、HTTPS 降级和非 identity 内容编码会被拒绝。其他引擎直接把 URL 交给搜索站点抓取。

成功搜索的 `structuredContent` 包含：

| 字段 | 说明 |
| --- | --- |
| `engine` | 实际使用的引擎 |
| `result_count` | 上游返回的总条数 |
| `returned` | 本次返回的条数 |
| `truncated` | 是否按 `limit` 截断 |
| `results` | 每项含 `title`、`url`、`thumbnail`、`similarity`，缺失值为 null |

相似度的单位和格式因引擎而异。引擎专属字段保留在文本结果中。

All 模式还返回 `engine_outcomes`，记录每个引擎的 `success`、`no_results` 或 `error` 状态和错误信息。汇总 `results` 中的每项增加 `engine` 字段；汇总条数为各引擎之和，未去重。`all_failed` 表示是否全部失败。

部分引擎失败时，其他引擎的结果照常返回；只有全部失败时，整次调用才返回 `isError=true`。空结果仍视为成功。

没有匹配结果时返回成功状态和空列表；输入、配置或明确的上游错误返回 `isError=true`。空结果不一定表示需要 Cookies，可先检查图片是否可访问、是否被裁剪，再尝试其他引擎。

### `get_engine_info`

`engine_name` 默认为 `all`，返回全部引擎的简要说明。传入引擎名时返回该引擎的参数、默认值及支持状态。

```json
{
  "engine_name": "SauceNAO"
}
```

## Linux 部署脚本

`image_search_mcp_deploy.sh` 适用于使用 apt 和 systemd 的 Linux 系统，通过 uv 安装并运行服务：

```bash
bash image_search_mcp_deploy.sh
bash image_search_mcp_deploy.sh --update-only
bash image_search_mcp_deploy.sh --reconfigure
bash image_search_mcp_deploy.sh --replace-unit
```

默认单元名为 `image-search-mcp`，可用 `IMAGE_SEARCH_UNIT` 覆盖。`--update-only` 升级包并重启现有服务；`--reconfigure` 备份并重写当前目录下的 `.env`，保留非托管配置；`--replace-unit` 还会备份并替换 systemd 单元。

已有单元默认保留。使用自行编写的单元时，请核对其 `ExecStart` 与 `EnvironmentFile` 是否使用脚本管理的工具和配置文件。

`.env` 按单行 `KEY=value` 格式读取，支持单引号与双引号；变量引用和命令替换均按字面值处理。配置与备份文件应妥善保管。

配置流程可选择默认引擎，也可以直接设置 `IMAGE_SEARCH_DEFAULT_ENGINE`。默认值在每次调用时读取；显式传入的 `engine` 优先。

## 开发

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
./check.sh
```

`check.sh` 运行单元测试、检查脚本自检和反向测试。`--fast` 跳过反向测试，`--reverse-only` 只运行反向测试。检查脚本需要 Bash 和 Git；Python 测试可直接用 `python -m pytest` 运行。

测试默认拦截真实 HTTP 与 DNS 请求。反向测试在临时副本中重新引入已修复的缺陷，检查对应测试能否发现问题。

| 命令 | 用途 |
| --- | --- |
| `python tools/reverse_tests/run.py --list` | 查看故障注入清单 |
| `python tools/status.py --update docs/实施进度.md` | 更新文档中的测试统计 |
| `tools/build_release.sh --check` | 构建 wheel 与 sdist，并检查重复构建的哈希；需要 uv 和干净的 Git 工作区 |

运行依赖以 `pyproject.toml` 为准。PicImageSearch 的受支持范围为 `>=3.12.11,<3.13`，FastMCP 为 `>=4.0.10,<5`；升级范围前需通过参数契约、网络集成和 MCP 协议测试。部署时可另行锁定确切版本。

发布前运行完整检查与构建验证，再用隔离环境安装生成的 wheel。确认版本号尚未发布后，使用 `twine upload dist/*` 上传。

后续计划见 [ROADMAP](docs/ROADMAP.md)，检查范围见 [实施进度](docs/实施进度.md)。旧审阅报告存放在 [历史文档](docs/archive/README.md)。

## License

MIT
