# CHANGELOG.md

本文件记录 **Grok2API 增强版** 相对上游的重要变更。

## 2026-03-19

### 新增
- 新增 `POST /v1/admin/tokens/append`
  - 提供非覆盖式 token 追加/更新能力
  - 适用于客户端、脚本、自动化任务增量推送 token
- 新增 `ENHANCEMENTS.md`
  - 记录增强版定位、维护规则、部署与同步注意事项
- README 新增增强版说明
  - 增强能力概览
  - 与上游差异说明
  - 客户端快速集成示例
  - 上游同步建议
  - append 接口文档章节

### 变更
- 管理界面的 token 导入逻辑改为直接调用 `/v1/admin/tokens/append`
  - 不再通过前端 merge 后再整体调用 `/v1/admin/tokens`
  - 导入行为与增强版 append 语义保持一致
- Docker Compose 维护经验明确：本地增强代码若要进容器，应优先使用本地 `build: .`，不能只依赖远程镜像
- README 默认 clone 地址改为增强版仓库：
  - `https://github.com/StardustWhisper/Grok2API.git`

### 维护说明
- 推荐仓库关系：
  - `origin` → `StardustWhisper/Grok2API`
  - `upstream` → `chenyme/grok2api`
- 推荐维护目录：
  - `/home/ubuntu/.openclaw/workspace/services/Grok2API-enhanced`
