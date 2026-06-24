"""Google Gemini "Nano Banana Pro" image generation (app/integrations/gemini_images).

Used by the personal-branding feature: we hand the model a STYLE/TEMPLATE reference image
plus the agent's PROFILE PHOTO and a text instruction, and it returns a new image that
restyles the agent to match the template (preserving their face) with an optional overlay.

This is the AI Studio / Generative Language API (NOT Vertex) via the `google-genai` SDK,
authenticated with GOOGLE_AI_STUDIO_API_KEY. The SDK is sync, so — like bedrock_images.py —
the blocking call is wrapped in asyncio.to_thread.

IMPORTANT: the Gemini *image* models have NO free tier. The Google project behind the key
must have billing enabled, or every call fails 429 RESOURCE_EXHAUSTED (free-tier limit: 0).
We surface that as a clear, actionable GeminiImageError(kind="quota").
"""
import asyncio
import logging

from app.config import settings

log = logging.getLogger("askalpha.gemini_images")


class GeminiImageError(Exception):
    """Image generation failed. `kind` lets callers tailor the user-facing message:
    'config'  — key missing / SDK not installed
    'quota'   — 429 RESOURCE_EXHAUSTED (no billing, or rate-limited)
    'blocked' — the model refused / returned no image (safety, recitation)
    'overloaded' — 503 UNAVAILABLE / 'high demand': model is busy (transient — retry/fallback)
    'api'     — any other upstream/transport error
    """

    def __init__(self, message: str, kind: str = "api"):
        super().__init__(message)
        self.kind = kind


def configured() -> bool:
    return bool(settings.google_ai_studio_api_key)


def _client():
    """Build an AI-Studio genai client. Lazy-imports the SDK so a missing dependency never
    breaks app import (only this feature)."""
    if not settings.google_ai_studio_api_key:
        raise GeminiImageError("GOOGLE_AI_STUDIO_API_KEY is not configured.", kind="config")
    try:
        from google import genai
        from google.genai import types
    except Exception as e:  # pragma: no cover - import guard
        raise GeminiImageError(f"google-genai SDK not available: {e}", kind="config") from e
    http_options = types.HttpOptions(timeout=settings.gemini_image_timeout_sec * 1000)  # ms
    return genai.Client(api_key=settings.google_ai_studio_api_key, http_options=http_options), types


def _is_transient_error(e: Exception) -> bool:
    """503 UNAVAILABLE / 'high demand' — the model server is busy. Worth retrying on the
    fallback model instead of failing the whole request."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    s = str(e).lower()
    return code == 503 or "503" in s or "unavailable" in s or "overloaded" in s or "high demand" in s


def _is_quota_error(e: Exception) -> bool:
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    s = str(e)
    # 429 RESOURCE_EXHAUSTED = no billing / rate-limit; 403 FAILED_PRECONDITION = billing not
    # enabled. Both are "fix the Google project's billing", not transient.
    return code == 429 or "RESOURCE_EXHAUSTED" in s or "429" in s or "FAILED_PRECONDITION" in s


def _generate(
    prompt: str,
    images: list[tuple[bytes, str]],
    aspect_ratio: str,
    image_size: str,
    model: str,
) -> bytes:
    """Blocking call. `images` is a list of (bytes, mime_type) in prompt order. Returns the
    generated image bytes (the SDK already base64-decodes inline_data.data)."""
    client, types = _client()

    contents = [types.Part.from_bytes(data=data, mime_type=mime) for data, mime in images]
    contents.append(prompt)

    config = types.GenerateContentConfig(
        response_modalities=["TEXT", "IMAGE"],
        image_config=types.ImageConfig(aspect_ratio=aspect_ratio, image_size=image_size),
    )

    try:
        resp = client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )
    except GeminiImageError:
        raise
    except Exception as e:
        if _is_transient_error(e):
            raise GeminiImageError(f"{type(e).__name__}: {str(e)[:200]}", kind="overloaded") from e
        if _is_quota_error(e):
            raise GeminiImageError(
                "Gemini image quota exhausted. The Nano Banana image models have no free tier — "
                "enable billing on the Google AI Studio project for this API key.",
                kind="quota",
            ) from e
        raise GeminiImageError(f"{type(e).__name__}: {str(e)[:200]}", kind="api") from e

    candidates = getattr(resp, "candidates", None) or []
    if not candidates:
        raise GeminiImageError("Model returned no candidates (request may have been blocked).", kind="blocked")

    parts = getattr(candidates[0].content, "parts", None) or []
    for part in parts:
        if getattr(part, "thought", False):
            continue
        inline = getattr(part, "inline_data", None)
        if inline and getattr(inline, "data", None):
            return inline.data

    finish = getattr(candidates[0], "finish_reason", None)
    raise GeminiImageError(
        f"Model returned no image (finish_reason={finish}). It may have been blocked by a "
        "safety filter — try a different photo or template.",
        kind="blocked",
    )


async def generate_branding_image(
    template_bytes: bytes,
    profile_bytes: bytes,
    prompt: str,
    aspect_ratio: str = "4:5",
    image_size: str | None = None,
    template_mime: str = "image/jpeg",
    profile_mime: str = "image/jpeg",
) -> bytes:
    """Generate one personal-branding image. Template is the FIRST reference image, the agent's
    profile photo the SECOND — the prompt references them by that order. Raises GeminiImageError."""
    size = image_size or settings.gemini_image_size
    images = [(template_bytes, template_mime), (profile_bytes, profile_mime)]
    # Try the primary model, then the fallback model when the primary is overloaded/unavailable
    # (the pro *preview* frequently 503s under load), quota-limited, or hits a transport error.
    # A 'config' (bad/missing key) or 'blocked' (safety) failure is NOT retried — the fallback
    # can't help and we want to surface those clearly.
    models: list[str] = []
    for m in (settings.gemini_image_model_id, settings.gemini_image_fallback_model_id):
        m = (m or "").strip()
        if m and m not in models:
            models.append(m)
    last: GeminiImageError | None = None
    for idx, model in enumerate(models):
        has_fallback = idx + 1 < len(models)
        try:
            return await asyncio.to_thread(_generate, prompt, images, aspect_ratio, size, model)
        except GeminiImageError as e:
            last = e
            if e.kind in ("config", "blocked") or not has_fallback:
                raise
            log.warning("branding: model %r failed (kind=%s); falling back to %r",
                        model, e.kind, models[idx + 1])
        except Exception as e:  # pragma: no cover - safety net
            last = GeminiImageError(f"{type(e).__name__}: {str(e)[:200]}", kind="api")
            if not has_fallback:
                raise last from e
            log.warning("branding: model %r errored (%s); falling back to %r",
                        model, type(e).__name__, models[idx + 1])
    raise last or GeminiImageError("Image generation failed.", kind="api")
