# ENHANCEMENTS.md

本文件记录 **Grok2API 增强版** 相比上游 `chenyme/grok2api` 的定制内容、维护约定和后续注意事项。

## 项目定位

本仓库是：
- **基于上游 `chenyme/grok2api` 的增强版本**
- 用于承载本地定制功能、接口扩展与持续维护

推荐 Git 关系：
- `origin` → 本增强版仓库
- `upstream` → `https://github.com/chenyme/grok2api`

推荐维护目录：
- `/home/ubuntu/.openclaw/workspace/services/Grok2API-enhanced`

## 已完成增强

### 1. 非覆盖式 Token 追加接口
新增接口：
- `POST /v1/admin/tokens/append`

目的：
- 让外部客户端可以 **增量推送 token**
- 避免原生 `/v1/admin/tokens` 的全量覆盖语义

接口语义：
- 只追加/更新，不覆盖其他 pool
- 已存在 token：按同 token merge 更新
- 不存在 token：追加

支持的请求形式：

#### 指定 pool
```json
{"pool":"default","token":"xxx"}
```

或

```json
{"pool":"default","tokens":[{"token":"xxx"}]}
```

#### 多 pool 批量
```json
{
  "default": [{"token":"a"}],
  "backup": [{"token":"b"}]
}
```

鉴权方式：
- 走后台 `app_key` 的 **Bearer 鉴权**
- 不是 query 参数 `?app_key=...`

示例：
```bash
curl -X POST 'http://HOST:PORT/v1/admin/tokens/append' \
  -H 'Authorization: Bearer <APP_KEY>' \
  -H 'Content-Type: application/json' \
  -d '{"pool":"default","token":"your_token_here"}'
```

### 2. 图片生成：从旧 WebSocket imagine 迁移到 app-chat REST

背景：
- Grok 已废弃旧 WebSocket 生图端点（wss://grok.com/ws/imagine/listen），继续使用会稳定失败（常见为 rate_limit_exceeded/blocked/无最终图）。
- 现已切换为网页端同款的 app-chat REST 流程。

改动：
- /v1/images/generations：从 ws_imagine 改为 app-chat REST（/rest/app-chat/conversations/new）
- 支持将 OpenAI 的 n 映射为 imageGenerationCount=n
- response_format=b64_json：自动下载图片并转 base64 返回（兼容 DALI 默认行为）

关键文件：
- app/services/grok/services/image_rest.py（REST 生图实现）
- app/services/grok/services/image.py（入口改为优先 REST）
- app/services/reverse/app_chat.py（新增 image_generation_count 参数，写入 imageGenerationCount）
- main.py：启动日志明确打印 Image generation backend=app-chat REST（防回退）
- scripts/selftest_images.sh：最小自测脚本（验收 data[0].b64_json 非空）

验收：
```bash
cd /home/ubuntu/.openclaw/workspace/services/Grok2API-enhanced
API_KEY=xxxx BASE_URL=https://xai.lambda.xin ./scripts/selftest_images.sh
```

部署注意：
- docker-compose.yml 必须使用本地 build（build: .），否则容器仍跑远程镜像看不到本地改动。
- 由于 proxy.base_proxy_url="socks5://warp:1080"，需保留 warp 服务；移除 warp 前必须先改配置再回归测试。


## 关键认知

### 管理界面的“导入”不是后端原生 append
原项目管理界面里的 token 导入，在体验上像 append，但实现方式是：
1. 前端先 `GET /v1/admin/tokens`
2. 浏览器本地 merge 新 token
3. 再整体 `POST /v1/admin/tokens`

也就是说：
- **UI 层是 append 体验**
- **API 层原本是全量覆盖保存**

因此：
- 管理后台人工操作问题不大
- 但外部客户端集成时，需要真正的 append API

## 部署维护规则

### 1. 本地改代码必须使用本地 build
如果 `docker-compose.yml` 使用的是：
```yaml
image: ghcr.io/chenyme/grok2api:latest
```
那么本地源码改动不会进入容器。

为了让本地增强版代码生效，必须使用：
```yaml
build:
  context: .
image: grok2api-chenyme-local:latest
```

### 2. 修改后推荐重建方式
```bash
cd /home/ubuntu/.openclaw/workspace/services/Grok2API-enhanced
sudo docker compose up -d --build
```

### 3. Warp 代理保留策略
当前增强版部署中，已确认生效配置包含：
```toml
[proxy]
base_proxy_url = "socks5://warp:1080"
```

这表示：
- Grok2API 默认通过 `warp` 容器提供的 SOCKS5 代理出网
- Warp 当前不是单纯待命，而是实际参与请求链路

背景说明：
- Warp 最初是在一次 Grok2API 问题排查中部署的
- 当时最终发现根因是 token，而不是网络
- 但考虑到当前运行环境是 VPS / 机房 IP，保留 Warp 仍然有现实意义：
  - 机房 IP 直连上游服务更容易遇到风控、地区、线路质量、成功率波动等问题
  - Warp 可以作为默认的防御性网络出口层

当前维护结论：
- **默认先保留 Warp**
- 在没有明确证据证明“直连同样稳定甚至更稳”之前，不建议贸然移除

如果未来需要下线 Warp，必须按以下顺序操作：
1. 先移除配置中的 `base_proxy_url = "socks5://warp:1080"`
2. 做直连回归测试（chat / image / video / append 等）
3. 确认稳定后，再停掉 `warp` 容器

⚠️ 注意：
- 不能只停掉容器、不改配置
- 否则 Grok2API 仍会尝试连接 `socks5://warp:1080`，导致请求失败

### 4. API 语义分工
建议保持清晰分层：
- `/v1/admin/tokens` = 全量覆盖/整体同步
- `/v1/admin/tokens/append` = 非覆盖式增量追加

适用场景：
- 管理面板整体编辑：`/v1/admin/tokens`
- 外部客户端推 token：`/v1/admin/tokens/append`

## Git 维护规则

### 推荐同步上游流程
```bash
cd /home/ubuntu/.openclaw/workspace/services/Grok2API-enhanced
git fetch upstream
git checkout main
git merge upstream/main
# 解决冲突后
# git push origin main
```

### 重要经验：新仓库首推要从干净完整 clone 开始
曾经尝试从旧工作目录直接首推到新空仓库，遇到：
- `remote unpack failed: index-pack failed`
- `did not receive expected object ...`

最终验证：
- 不是 GitHub 权限问题
- 最稳的方案是：
  1. 从上游重新完整 clone
  2. 在干净 clone 上补增强
  3. 再推到新仓库

所以以后如果要新建增强版仓库，优先按这个流程做，不要直接拿历史状态不明的工作目录首推。

## 后续建议

可以继续做的增强：
1. 把管理界面的“导入”按钮直接改成走 `/v1/admin/tokens/append`
2. 增加追加后自动刷新状态的接口，例如：`append-and-refresh`
3. 增加 `CHANGELOG.md`，记录增强版相对上游的变化
4. 明确部署时统一使用哪个目录，避免多个目录长期漂移

## 维护原则

- 尊重并保留上游来源信息
- 优先保持与上游可同步
- 本地增强尽量做成清晰、最小、可维护的增量修改
- 新增接口时，尽量明确区分“覆盖式”与“增量式”语义，避免误用
