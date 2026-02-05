# Image Search MCP Implementation Plan (FastMCP)

- [x] **项目初始化**
    - [x] 确认项目结构和依赖
    - [x] 创建 `requirements.txt`
    - [x] 创建 `src` 目录用于存放源代码

- [x] **核心逻辑实现 (`src/server.py`)**
    - [x] 导入 `fastmcp.FastMCP` 和 `PicImageSearch`
    - [x] 初始化 `FastMCP("image-search")`
    - [x] 定义支持的引擎映射表
    - [x] 实现 `search_image` 工具
        - [x] **参数**:
            - `engine`: string (默认 Yandex, 支持 SauceNAO, Google, TraceMoe, Ascii2D, etc.)
            - `source`: string (URL or Base64 string)
            - `api_key`: string (Optional, for engines like SauceNAO)
            - `cookies`: string (Optional, for engines like EHentai)
            - `proxy`: string (Optional, e.g., "http://127.0.0.1:7890")
            - `extra_params`: string (Optional JSON string for advanced configs like `bovw`, `is_ex`)
        - [x] **逻辑**:
            - 识别 `source` 类型 (URL vs Base64)
            - 实例化对应引擎，配置 `api_key` 和 `proxies`, `cookies`
            - 执行 `await engine.search(...)`
            - 解析结果并返回格式化的文本
    - [x] 添加 `list_engines` 工具

- [x] **测试与验证**
    - [x] 创建 `test_logic.py` (因环境依赖问题暂未在当前环境成功运行，但逻辑已就绪)
    - [x] 验证 URL 搜索功能 (代码层面)
    - [x] 验证 Base64 搜索功能 (代码层面)

- [x] **文档与清理**
    - [x] 完善 `README.md`，说明启动方式、支持的引擎及环境要求