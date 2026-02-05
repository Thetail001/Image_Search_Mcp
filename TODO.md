# Image Search MCP Implementation Plan (FastMCP)

- [ ] **项目初始化**
    - [x] 确认项目结构和依赖
    - [x] 创建 `requirements.txt`
    - [ ] 创建 `src` 目录用于存放源代码

- [ ] **核心逻辑实现 (`src/server.py`)**
    - [ ] 导入 `mcp.server.fastmcp.FastMCP` 和 `PicImageSearch`
    - [ ] 初始化 `FastMCP("image-search")`
    - [ ] 定义支持的引擎映射表
    - [ ] 实现 `search_image` 工具
        - [ ] **参数**:
            - `engine`: string (Supported: SauceNAO, Google, TraceMoe, Ascii2D, etc.)
            - `source`: string (URL or Base64 string)
            - `api_key`: string (Optional, for engines like SauceNAO)
            - `proxy`: string (Optional, e.g., "http://127.0.0.1:7890")
            - `extra_params`: string (Optional JSON string for advanced configs like `numres`, `db`)
        - [ ] **逻辑**:
            - 识别 `source` 类型 (URL vs Base64)
            - 实例化对应引擎，配置 `api_key` 和 `proxies`
            - 执行 `await engine.search(...)`
            - 解析结果并返回格式化的文本或 JSON 结构
    - [ ] 添加 `list_engines` 工具
        - [ ] 返回所有支持的搜索引擎名称及其说明

- [ ] **测试与验证**
    - [ ] 创建 `test_client.py` 或使用 `mcp dev` 进行交互测试
    - [ ] 验证 URL 搜索功能
    - [ ] 验证 Base64 搜索功能

- [ ] **文档与清理**
    - [ ] 完善 `README.md`，说明启动方式和支持的引擎
