# 图片生成修复记录 (2026-03-29)

## 问题

Grok 图片生成接口返回 502 错误：`Image generation returned empty data`，所有生图请求全部失败。

## 根因

xAI 在 2026 年 3 月对 Grok 的图片生成链路做了两个重大变更：

### 1. `modelMode` 参数变更

- **之前**: 请求不需要 `modelMode`，传 `None` 即可触发图片生成
- **之后**: 必须传 `modelMode: "MODEL_MODE_FAST"` 才会走图片生成路径
- **影响**: 不传 mode 时，Grok 默认走文本聊天模式，返回 web 搜索到的图片（`render_searched_image`），而非 AI 生成的图片（`render_generated_image`）

### 2. 图片 URL 存储位置变更

- **之前**: 图片 URL 在 `modelResponse.generatedImageUrls` 数组中
- **之后**: 图片 URL 移到了两个新位置：
  - 流式事件: `cardAttachment.jsonData.image_chunk.imageUrl`（进度 50% / 100%）
  - 最终结果: `modelResponse.cardAttachmentsJson[].image_chunk.imageUrl`（JSON 字符串数组）
- 同时 `generatedImageUrls` 变为空数组 `[]`

### 3. 半成品图片问题

- 流式响应中先返回 `seq: 0, progress: 50` 的 `-part-0` 半成品图
- 再返回 `seq: 1, progress: 100` 的最终图
- 旧的 `_collect_images` 函数会同时收集两个 URL，取第一个（半成品）导致图片不完整

## 修复方案

### 上游合并

- 合并 `chenyme/grok2api` 最新代码（PR #374 等）
- 新增 `modelMode` 参数支持（`MODEL_MODE_FAST`）
- 新增 `request_overrides` 参数替代 `image_generation_count`
- 新增 ws_imagine 降级回退机制
- 删除废弃的 `image_rest.py`，逻辑合并到 `image.py` + `image_edit.py`

### 本地修复

1. **`app/services/grok/utils/process.py`** — `_collect_images` 函数：
   - 新增 `imageUrl` 键搜索（dict 中递归查找）
   - 新增 JSON 字符串解析（`cardAttachmentsJson` 条目是 JSON 字符串）
   - 过滤 `-part-` URL（丢弃半成品图片）

## 验证

```bash
# API 直接测试
curl -X POST http://10.0.0.82:8012/v1/images/generations \
  -H "Authorization: Bearer $API_KEY" \
  -d '{"model":"grok-imagine-1.0","prompt":"a cute cat","n":1,"response_format":"b64_json"}'
# → HTTP 200, base64 数据完整

# OpenClaw 脚本测试
node skills/grok2api-image/scripts/imagine.mjs --prompt "..." --size 1024x1024
# → 文件大小 282KB，图片完整
```

## 关键教训

- xAI 的 Grok 逆向接口经常变动，图片生成尤其不稳定
- `cardAttachmentsJson` 是新的图片数据载体，格式为 JSON 字符串数组
- 流式响应中 `-part-` 后缀的 URL 是半成品，必须过滤
- `modelMode` 是触发图片生成的关键参数，不传则降级为搜索模式
- 上游 chenyme/grok2api 活跃维护中，应定期同步

## Git 提交记录

```
f233a03 fix: filter out partial image URLs (-part-) to avoid truncated results
e32b0f4 fix: parse cardAttachmentsJson for image URLs (upstream API format change)
7fa73c8 Merge upstream: fix image generation (MODEL_MODE_FAST) and ws_imagine fallback
```
