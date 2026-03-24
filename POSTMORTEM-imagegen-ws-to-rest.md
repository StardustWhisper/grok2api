# Postmortem: Grok2API image generation 502 (WS imagine deprecated) → migrate to app-chat REST

## Summary
- Symptom: `POST /v1/images/generations` (model=`grok-imagine-1.0`) returned **502 upstream_error** with message **"Image generation blocked or no valid final image"**.
- Root cause: Grok **deprecated the old WebSocket imagine endpoint** (`wss://grok.com/ws/imagine/listen`). The WS stream frequently returns `rate_limit_exceeded` / `blocked` / no valid final image.
- Fix: Migrate image generation implementation from **ws_imagine** to **app-chat REST** (`/rest/app-chat/conversations/new`), matching current web behavior.

## Detection & Evidence
- `GET /v1/models` showed image models present (`grok-imagine-1.0`, fast/edit/video), so mapping was fine.
- Direct curl with valid API key reproduced the 502.
- Container logs showed WS errors like:
  - `WebSocket error: rate_limit_exceeded - Image rate limit exceeded`
  - Followed by recovery attempts and final failure.

## Resolution
- Implemented REST image generation via app-chat:
  - Endpoint: `https://grok.com/rest/app-chat/conversations/new`
  - Enable tool: `toolOverrides: { imageGen: true }`
  - Map OpenAI `n` → `imageGenerationCount`
  - Parse images from `result.response.modelResponse` (recursive keys: `generatedImageUrls` / `imageUrls` / `imageURLs`).
  - If `response_format=b64_json`, download image and base64 encode.

## Compatibility Notes
- Must support `response_format=b64_json` (DALI default) even if upstream returns URLs.
- Support sizes: `1024x1024`, `1024x1792`, `1792x1024` (mapped to aspect ratios when possible).
- Avoid infinite retries; keep 1–2 token retries.

## Deployment Gotchas
- **Local code changes do not apply** if docker-compose uses remote image only.
  - Must use `build: .` and rebuild.
- If `proxy.base_proxy_url="socks5://warp:1080"` is configured, keep `warp` service enabled.

## Verification
- Added startup log:
  - `Image generation backend: app-chat REST (/rest/app-chat/conversations/new)`
- Added self-test script:
  - `scripts/selftest_images.sh`
- Acceptance: `POST /v1/images/generations` returns **200** and `data[0].b64_json` is non-empty.

## Prevent Recurrence
- Keep self-test as part of upgrade checklist.
- Keep backend log line to quickly spot unintended regressions.
