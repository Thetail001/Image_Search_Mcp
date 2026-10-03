# 评审任务：image-search-mcp 代码评审（只评审，不要改任何文件）

你是在帮一位自托管 AI agent 的重度用户做代码评审。**请只出评审意见，禁止修改、新建、删除任何文件**（读文件、跑只读命令都可以）。

## 仓库

当前工作目录就是这个仓库的本地克隆：`Image_Search_Mcp`（PyPI 包名 `image-search-mcp`，版本 0.2.1，作者 Thetail001）。
它是个 MCP server，封装 [PicImageSearch](https://github.com/kitUIN/PicImageSearch) 做多引擎以图搜图，同时支持 stdio 和 SSE(HTTP) 两种传输。

代码很小：`src/image_search_mcp/server.py`（384 行，核心逻辑）、`main.py`（111 行，CLI + Bearer 认证中间件）。另有 `README.md`、`TODO.md`、`pyproject.toml`、`requirements.txt`、`image_search_mcp_deploy.sh`。

## 生产环境实况（重要，评安全时请纳入）

它真实部署在一台公网 VPS 上，不是玩具：

- systemd 单元 `image-search-mcp.service`，`WorkingDirectory=/opt/docker/ImageSearchMcp`
- `ExecStart` 走一个 `start.sh`，内容是 `exec uvx image-search-mcp --sse --host 0.0.0.0 --port 8000`
- **以 root 运行，绑定 0.0.0.0，公网可直达**
- 环境变量 `MCP_AUTH_TOKEN` 是唯一凭据（64 hex）；未授权请求走 401
- 日志里持续有外部 IP 扫描 `/sse`、`/mcp`、`/jsonrpc`、`/.well-known/security.txt` 等路径
- 被 SillyTavern（同机另一个服务）当工具调用
- 代码本体跑在 uvx 缓存里（`~/.cache/uv/archive-v0/...`），即**用户拿到的是 PyPI 上的构建产物，依赖没有锁版本**

## 我已用实测确认的问题（请你复核并判断严重性，不要只复述）

我用一个干净 venv 装了当前最新依赖实测：

```
fastmcp 4.0.10 · mcp 2.3.0 · PicImageSearch 3.12.11 · httpx 0.28.1 · starlette 1.7.0 · uvicorn 0.54.0
```

**A. cookies / proxy 两个环境变量一旦设置，所有搜索必定失败。**
`PicImageSearch.Network.__init__` 的真实签名是：

```python
def __init__(self, internal=False, proxies: Optional[str] = None, headers=None,
             cookies: Optional[str] = None, timeout=30, verify_ssl=True, http2=False)
```

`cookies` 和 `proxies` 都吃**字符串**。而本仓库 `server.py` 里：

- `_parse_cookies()` 把 cookie 串解析成 **dict** 再塞给 `Network(cookies=...)` → 实测 `AttributeError: 'dict' object has no attribute 'split'`
- `_parse_proxy()` 返回 `{"http://": ..., "https://": ...}` **dict** 再塞给 `Network(proxies=...)` → 实测 `AttributeError: 'dict' object has no attribute 'url'`
- 正确形态（`Network(proxies="http://…:7890", cookies="k=v; k2=v2")`）实测正常，`net.cookies` 能正确解析

而 README 恰恰把这两个变量写成「遇到机器人验证就这样配」的官方解法 ⇒ **照文档做的人 100% 撞死**，且错误信息被 `except Exception` 包成一句含糊的 `An error occurred during search: …`。

**B. `mcp.server.fastmcp` 这个 fallback 在 mcp 2.x 里已经不存在。**
`server.py` 的 `try: from fastmcp import FastMCP / except ImportError: from mcp.server.fastmcp import FastMCP`，
以及 `main.py` 的 `from mcp.server.fastmcp import create_sse_app`，实测均 `ModuleNotFoundError`（mcp 2.x 已把 FastMCP 改名 MCPServer，路径都变了）。

**C. `main.py` 的异常降级链会静默换成错误的传输。**
`mcp.http_app(transport="sse")` 在 fastmcp 4.0.10 实测可用（路由 `/sse` + `/messages`），所以主路径今天没坏。
但它被包在 `except` 里，降级会走 `mcp.http_app(path="/")` —— 那默认是 **streamable-http**，不是 SSE，客户端按 `/sse` 连会连不上，而日志里只会有一句 `Warning:`。

## 请你回答（按这个顺序，尽量具体、可执行）

1. **复核上面 A/B/C**：结论对不对？严重性怎么排？有没有我说错的地方？
2. **补我没看到的 bug**：正确性、并发/异步、超时缺失（每个引擎搜索都没有 timeout 上限？）、资源泄漏、`limit` 无边界、错误信息把 traceback 直接回给客户端（信息泄露）、token 比较是否用了非常量时间比较（时序攻击）、`AuthMiddleware` 只拦 `scope["type"]=="http"`（websocket 是否漏检）、`/` 免认证是否该保留。
3. **MCP 规范一致性**：SSE 在现行 MCP 里是过时传输吗？该不该默认改成 streamable-http？工具的返回值现在是**拼好的纯文本**——该不该改成结构化结果（JSON），让调用方（LLM）自己决定怎么渲染？请给出取舍，别只给结论。
4. **打包与依赖**：`pyproject.toml` 依赖完全不锁版本 + `requirements.txt` 重复声明；`license = MIT` 但仓库里没有 LICENSE 文件；`authors` 是 `Your Name / your.email@example.com` 占位符。这些对一个发到 PyPI 的包意味着什么？应该怎么改？
5. **测试**：现在**一个测试都没有**（`TODO.md` 自己写着「因环境依赖问题暂未在当前环境成功运行」）。请设计一套**不依赖真实外网**的测试方案：哪些点该 mock、哪些必须真跑、怎么测认证中间件、怎么测「cookie/proxy 传错类型」这类回归。给出测试文件结构和具体用例清单。
6. **优先级排序的改进清单**：每条写清「改哪个文件、改什么、为什么、怎么验证」。分开「必须立刻修（会害用户）」和「值得做」。

## 输出要求

- 直接给评审正文，不要客套、不要复述我上面写的内容充数。
- 结论要有依据（引用 `文件:行号`，或你实际跑出来的命令输出）。
- 结尾给一张**按优先级排的清单**，我会照着改。
- 中文回答。
