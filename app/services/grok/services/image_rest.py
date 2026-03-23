"""Grok image generation service (app-chat REST based).

This replaces the deprecated imagine WebSocket flow (wss://grok.com/ws/imagine/listen).
It uses the same app-chat reverse endpoint as chat/video/image_edit.

High-level flow:
- Build a chat request with toolOverrides.imageGen=True.
- Set imageGenerationCount=n.
- Optionally pass modelConfigOverride to steer aspect ratio.
- Stream the response and extract image URLs from result.response.modelResponse.
- Download each image and convert to base64 when response_format=b64_json.

Notes:
- The exact image model mapping is controlled server-side by Grok; the app-chat modelName is
  still a text model (e.g. grok-3) while image generation is enabled via toolOverrides.
- We keep behavior compatible with existing OpenAI images API exposed by grok2api.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncGenerator, AsyncIterable, Dict, List, Optional, Union

import orjson
from curl_cffi.requests.errors import RequestsError

from app.core.config import get_config
from app.core.exceptions import AppException, ErrorType, UpstreamException, StreamIdleTimeoutError
from app.core.logger import logger
from app.services.grok.services.chat import GrokChatService
from app.services.grok.utils.download import DownloadService
from app.services.grok.utils.process import _with_idle_timeout, _normalize_line, _collect_images, _is_http2_error
from app.services.grok.utils.retry import pick_token, rate_limited
from app.services.grok.utils.response import make_response_id, make_chat_chunk, wrap_image_content
from app.services.grok.utils.stream import wrap_stream_with_usage
from app.services.token import EffortType


ALLOWED_ASPECT_RATIOS = {"1:1", "2:3", "3:2", "9:16", "16:9"}


def _ratio_from_size(size: str) -> str:
    value = (size or "").strip() or "1024x1024"
    mapping = {
        "1024x1024": "1:1",
        "1024x1792": "2:3",
        "1792x1024": "3:2",
        "720x1280": "9:16",
        "1280x720": "16:9",
    }
    return mapping.get(value, "2:3")


def _build_image_model_config(aspect_ratio: str) -> Dict[str, Any]:
    """Best-effort modelConfigOverride for image generation.

    We mirror the style used by video_extend (modelMap.videoGenModelConfig).
    For image generation, Grok web payloads may differ; we keep this as optional and safe.
    """

    if not aspect_ratio or aspect_ratio not in ALLOWED_ASPECT_RATIOS:
        return {}

    # Conservative guess: some deployments accept modelMap.imageGenModelConfig
    # If Grok ignores unknown keys, this is harmless.
    return {
        "modelMap": {
            "imageGenModelConfig": {
                "aspectRatio": aspect_ratio,
            }
        }
    }


@dataclass
class ImageGenerationResult:
    stream: bool
    data: Union[AsyncGenerator[str, None], List[str]]
    usage_override: Optional[dict] = None


class ImageGenerationRestService:
    """Image generation orchestration via app-chat REST."""

    def __init__(self) -> None:
        self._dl: Optional[DownloadService] = None

    def _get_dl(self) -> DownloadService:
        if self._dl is None:
            self._dl = DownloadService()
        return self._dl

    async def close(self):
        if self._dl:
            await self._dl.close()
            self._dl = None

    @staticmethod
    def _effort(model_info: Any) -> EffortType:
        return (
            EffortType.HIGH
            if (model_info and getattr(model_info, "cost", None) and model_info.cost.value == "high")
            else EffortType.LOW
        )

    async def generate(
        self,
        *,
        token_mgr: Any,
        token: str,
        model_info: Any,
        prompt: str,
        n: int,
        response_format: str,
        size: str,
        aspect_ratio: str,
        stream: bool,
        enable_nsfw: Optional[bool] = None,
        chat_format: bool = False,
    ) -> ImageGenerationResult:
        if response_format == "base64":
            response_format = "b64_json"

        max_token_retries = int(get_config("retry.max_retry") or 3)
        tried_tokens: set[str] = set()
        last_error: Optional[Exception] = None

        # nsfw tag routing (reuse existing behavior)
        if enable_nsfw is None:
            enable_nsfw = bool(get_config("image.nsfw"))
        prefer_tags = {"nsfw"} if enable_nsfw else None

        # force n range
        n = int(n or 1)
        n = max(1, min(n, 10))

        # app-chat uses a text model name; in ModelService, grok_model for image models is grok-3
        grok_text_model = model_info.grok_model if model_info else "grok-3"

        tool_overrides = {"imageGen": True}
        # best-effort: allow aspect ratio steering via modelConfigOverride
        ratio = aspect_ratio or _ratio_from_size(size)
        model_config_override = _build_image_model_config(ratio)

        if stream:

            async def _stream_retry() -> AsyncGenerator[str, None]:
                nonlocal last_error
                for attempt in range(max_token_retries):
                    preferred = token if (attempt == 0 and not prefer_tags) else None
                    current_token = await pick_token(
                        token_mgr,
                        model_info.model_id,
                        tried_tokens,
                        preferred=preferred,
                        prefer_tags=prefer_tags,
                    )
                    if not current_token:
                        if last_error:
                            raise last_error
                        raise AppException(
                            message="No available tokens. Please try again later.",
                            error_type=ErrorType.RATE_LIMIT.value,
                            code="rate_limit_exceeded",
                            status_code=429,
                        )

                    tried_tokens.add(current_token)
                    yielded = False
                    try:
                        upstream = await GrokChatService().chat(
                            token=current_token,
                            message=prompt,
                            model=grok_text_model,
                            mode=None,
                            stream=True,
                            tool_overrides=tool_overrides,
                            model_config_override=model_config_override or None,
                            image_generation_count=n,
                        )
                        processor = ImageRestStreamProcessor(
                            model_info.model_id,
                            current_token,
                            n=n,
                            response_format=response_format,
                            size=size,
                            chat_format=chat_format,
                        )
                        async for chunk in wrap_stream_with_usage(
                            processor.process(upstream),
                            token_mgr,
                            current_token,
                            model_info.model_id,
                        ):
                            yielded = True
                            yield chunk
                        return
                    except UpstreamException as e:
                        last_error = e
                        if rate_limited(e):
                            if yielded:
                                raise
                            await token_mgr.mark_rate_limited(current_token)
                            continue
                        raise

                if last_error:
                    raise last_error
                raise AppException(
                    message="No available tokens. Please try again later.",
                    error_type=ErrorType.RATE_LIMIT.value,
                    code="rate_limit_exceeded",
                    status_code=429,
                )

            return ImageGenerationResult(stream=True, data=_stream_retry())

        # non-stream collect
        for attempt in range(max_token_retries):
            preferred = token if (attempt == 0 and not prefer_tags) else None
            current_token = await pick_token(
                token_mgr,
                model_info.model_id,
                tried_tokens,
                preferred=preferred,
                prefer_tags=prefer_tags,
            )
            if not current_token:
                if last_error:
                    raise last_error
                raise AppException(
                    message="No available tokens. Please try again later.",
                    error_type=ErrorType.RATE_LIMIT.value,
                    code="rate_limit_exceeded",
                    status_code=429,
                )

            tried_tokens.add(current_token)
            try:
                upstream = await GrokChatService().chat(
                    token=current_token,
                    message=prompt,
                    model=grok_text_model,
                    mode=None,
                    stream=True,
                    tool_overrides=tool_overrides,
                    model_config_override=model_config_override or None,
                    image_generation_count=n,
                )
                processor = ImageRestCollectProcessor(
                    model_info.model_id,
                    current_token,
                    response_format=response_format,
                )
                images = await processor.process(upstream)

                if len(images) < n:
                    raise UpstreamException(
                        "Image generation returned empty data.",
                        details={"error_code": "empty_image", "got": len(images), "requested": n},
                    )

                try:
                    await token_mgr.consume(current_token, self._effort(model_info))
                except Exception as e:
                    logger.warning(f"Failed to consume token: {e}")

                usage_override = {
                    "total_tokens": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "input_tokens_details": {"text_tokens": 0, "image_tokens": 0},
                }
                return ImageGenerationResult(stream=False, data=images[:n], usage_override=usage_override)

            except UpstreamException as e:
                last_error = e
                if rate_limited(e):
                    await token_mgr.mark_rate_limited(current_token)
                    continue
                raise

        if last_error:
            raise last_error
        raise AppException(
            message="No available tokens. Please try again later.",
            error_type=ErrorType.RATE_LIMIT.value,
            code="rate_limit_exceeded",
            status_code=429,
        )


class _ImageRestBase:
    def __init__(self, model: str, token: str = "", response_format: str = "b64_json"):
        if response_format == "base64":
            response_format = "b64_json"
        self.model = model
        self.token = token
        self.response_format = response_format
        self._dl: Optional[DownloadService] = None

    def _get_dl(self) -> DownloadService:
        if self._dl is None:
            self._dl = DownloadService()
        return self._dl

    async def close(self):
        if self._dl:
            await self._dl.close()
            self._dl = None

    async def _to_output(self, url: str) -> str:
        if not url:
            return ""
        if self.response_format == "url":
            # For url mode, normalize to local /v1/files if app_url is set
            return await self._get_dl().resolve_url(url, self.token, "image")
        # b64_json
        data_uri = await self._get_dl().parse_b64(url, self.token, "image")
        if not data_uri:
            return ""
        if "," in data_uri:
            return data_uri.split(",", 1)[1]
        return data_uri


class ImageRestCollectProcessor(_ImageRestBase):
    """Collect final images from app-chat stream."""

    async def process(self, response: AsyncIterable[bytes]) -> List[str]:
        images: List[str] = []
        idle_timeout = float(get_config("image.stream_timeout") or 60)

        try:
            async for line in _with_idle_timeout(response, idle_timeout, self.model):
                line = _normalize_line(line)
                if not line:
                    continue
                try:
                    data = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue

                resp = data.get("result", {}).get("response", {})
                if mr := resp.get("modelResponse"):
                    urls = _collect_images(mr)
                    for url in urls:
                        out = await self._to_output(url)
                        if out:
                            images.append(out)
        except asyncio.CancelledError:
            raise
        except StreamIdleTimeoutError as e:
            raise UpstreamException(
                message=f"Image collect idle timeout after {e.idle_seconds}s",
                status_code=504,
                details={"type": "stream_idle_timeout", "idle_seconds": e.idle_seconds},
            )
        except RequestsError as e:
            if _is_http2_error(e):
                raise UpstreamException(
                    message="Upstream connection closed unexpectedly",
                    status_code=502,
                    details={"type": "http2_stream_error", "error": str(e)},
                )
            raise UpstreamException(
                message=f"Upstream request failed: {e}",
                status_code=502,
                details={"type": "upstream_request_failed", "error": str(e)},
            )
        finally:
            await self.close()

        return images


class ImageRestStreamProcessor(_ImageRestBase):
    """Stream wrapper. For now we only emit final images, mirroring existing behavior."""

    def __init__(
        self,
        model: str,
        token: str = "",
        n: int = 1,
        response_format: str = "b64_json",
        size: str = "1024x1024",
        chat_format: bool = False,
    ):
        super().__init__(model, token, response_format)
        self.n = int(n or 1)
        self.size = size
        self.chat_format = bool(chat_format)
        self._id_generated = False
        self._response_id: str = ""

    def _sse(self, event: str, data: dict) -> str:
        return f"event: {event}\ndata: {orjson.dumps(data).decode()}\n\n"

    async def process(self, response: AsyncIterable[bytes]) -> AsyncGenerator[str, None]:
        # We don't have partial base64s; only progress events.
        final_urls: List[str] = []
        idle_timeout = float(get_config("image.stream_timeout") or 60)

        try:
            async for line in _with_idle_timeout(response, idle_timeout, self.model):
                line = _normalize_line(line)
                if not line:
                    continue
                try:
                    data = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue

                resp = data.get("result", {}).get("response", {})

                if img := resp.get("streamingImageGenerationResponse"):
                    # Only emit progress for image_generation (not chat_format)
                    if not self.chat_format:
                        idx = int(img.get("imageIndex", 0))
                        progress = img.get("progress", 0)
                        if self.n == 1:
                            idx = 0
                        yield self._sse(
                            "image_generation.partial_image",
                            {
                                "type": "image_generation.partial_image",
                                "index": idx,
                                "progress": progress,
                            },
                        )
                    continue

                if mr := resp.get("modelResponse"):
                    urls = _collect_images(mr)
                    for url in urls:
                        if url and url not in final_urls:
                            final_urls.append(url)

            # emit finals
            for i, url in enumerate(final_urls[: self.n]):
                output = await self._to_output(url)
                if not output:
                    continue

                if self.chat_format:
                    if not self._id_generated:
                        self._response_id = make_response_id()
                        self._id_generated = True
                    out = wrap_image_content(output, self.response_format)
                    yield self._sse(
                        "chat.completion.chunk",
                        make_chat_chunk(
                            self._response_id,
                            self.model,
                            out,
                            index=(0 if self.n == 1 else i),
                            is_final=True,
                        ),
                    )
                else:
                    field = "url" if self.response_format == "url" else "b64_json"
                    yield self._sse(
                        "image_generation.completed",
                        {
                            "type": "image_generation.completed",
                            field: output,
                            "created_at": 0,
                            "size": self.size,
                            "index": (0 if self.n == 1 else i),
                            "stage": "final",
                            "usage": {
                                "total_tokens": 0,
                                "input_tokens": 0,
                                "output_tokens": 0,
                                "input_tokens_details": {"text_tokens": 0, "image_tokens": 0},
                            },
                        },
                    )

            if self.chat_format:
                if not self._id_generated:
                    self._response_id = make_response_id()
                    self._id_generated = True
                yield "data: [DONE]\n\n"

        finally:
            await self.close()
