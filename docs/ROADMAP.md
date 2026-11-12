# 后续计划

当前版本已实现参数校验、HTTP 认证、按引擎隔离凭据、安全图片下载、结构化结果，以及本机 stdio 模式下的受控本地图片输入。实现与检查范围见 [实施进度](实施进度.md)。

## 优先处理

- [ ] 限制服务整体的并发请求数。当前已限制单次搜索的总期限，以及每次 All 查询的并发数。
- [x] 补充引擎错误分类：上游解析失败（`UpstreamParsingError`）、响应缺字段（`KeyError`/`IndexError`）、上游 HTTP 状态（含响应体取证）、源图抓取失败（`DownloadError`）已分别给出可判读文案。限流与验证页面尚未单独识别，仍归在"上游 HTTP 状态"里。
- [ ] 限制结果文本与上游字段长度，避免少量结果产生过大的工具输出。
- [x] 检查日志脱敏：`_redact_upstream` 覆盖 data URI 与 `api_key`/`token`/`secret` 等参数；上游 4xx 的响应体只截取 200 字并先脱敏，不记 URL。
- [ ] 为依赖升级增加定期检查，验证候选版本后再调整支持范围。

## 可用性

- [ ] 验证第三方 MCP 客户端的 stdio 与 SSE 连接，以及文本和结构化结果的兼容性。
- [ ] 评估 Streamable HTTP 支持，明确客户端配置和迁移方式。
- [x] ~~在能明确识别源图抓取失败后，评估 Yandex 的 URL 转上传重试。~~ **2026-11-13 实测证否，不做。** 理由：Yandex 结果页已改为前端渲染，首屏 `cbirSites.sites=[]`、`cbirIntent.status="loading"`；`url` 与 `file` 两种输入都抛 `UpstreamParsingError`；`format=json` 只回空骨架（`{"blocks":[null]}`）、`images-apphost` 返回 404、上传端点要求一个我们无法生成的 `cbird`。纯 HTTP 拿不到结果，转上传重试没有任何收益。
- [x] 补充各引擎的在线验证记录，注明时间、图片和网络条件 —— 已落地为 `tools/engine_health.py`（看门狗），2026-11-13 首轮记录见下。
- [ ] 评估 AnimeTrace、Copyseeker 的接入成本，包括参数、结果映射和错误处理。

### 引擎在线可用性（2026-11-13 实测，从德国 de 出口，测试图＝维基埃菲尔铁塔）

| 引擎 | 结果 | 现象 |
| --- | --- | --- |
| BaiDu | 可用 | 8 条；本服务自己取图后上传 |
| Yandex | 可用但限流敏感 | 静置后间隔 8 秒连打 4/4 成功、每次 81 条；短时间连打则返回「预搜索」空壳（`cbirSites.sites=[]`、`cbirIntent.status="loading"`）→ `UpstreamParsingError` |
| Tineye | 可用 | 10 条；偶发 `KeyError('query_hash')` |
| Iqdb | 限流敏感 | 被判定机器人后 302 → `botcheck=failed`，`IndexError` |
| Bing | 不可用 | 上传流程改版，`ValueError('BCID not found on page')` |
| GoogleLens / Google / SauceNAO | 空结果 | url 与 file 两种输入都是 0 条；SauceNAO 网页路径被 Cloudflare 挑战（403 `cf-mitigated: challenge`）挡住 |

**踩坑记录（重要，别再判错）**：一开始把所有引擎一次性连打 64 次（4 图 × 8 引擎 × 2 模式，间隔仅 1 秒），Yandex 连续 5 次失败，于是误判成"上游改版永久失效"。静置半小时后同样的图和代码 4/4 全成功——真正原因是**自己的请求密度把上游惹出了软限流**。这类"我们自己造成"的故障，单次探测永远看不出来，必须在低频率下复测才能定论。

因此 `DEFAULT_ENGINE` 由 `Yandex` 改为 `BaiDu`（单次调用稳定，不受限流影响）；Yandex 仍可显式选用。`get_engine_info` 会把上表里的已知问题直接标在引擎条目后面。

PicImageSearch 侧对限流无能为力：它不区分"空壳页"和"改版"，都报 `ParsingError`；PyPI 最新仍是 3.12.11（2025-08 发布），`main` 分支的 `model/yandex.py` 与 `engines/yandex.py` 和 3.12.11 一致；其测试用 VCR 录播（`@pytest.mark.vcr("yandex_url_search.yaml")`），回放旧页面所以 CI 一直是绿的。

## 部署与发布

- [ ] 在隔离机器上验证完整部署、更新与回滚流程。
- [ ] 明确已有 systemd 单元使用自定义启动脚本时的更新行为。
- [ ] 补充低权限运行、依赖锁定和 HTTPS 反向代理示例。
- [ ] 增加安装 wheel 后的真实进程测试，覆盖启动失败、健康检查与认证连接。

历史计划和原始审阅记录见 [archive](archive/README.md)。已完成的条目不再重复列入本文件。
