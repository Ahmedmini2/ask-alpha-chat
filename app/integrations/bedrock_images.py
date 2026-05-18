"""Bedrock-backed text-to-image generation for HeyGen video backgrounds.

Uses Stability AI on Bedrock (us-west-2) since Amazon's Titan/Nova image generators
are marked legacy and not granular in IAM/Model Access for new accounts.
"""
import asyncio
import base64
import json
import boto3
from app.config import settings

_bedrock = boto3.client("bedrock-runtime", region_name=settings.bedrock_image_region)


class ImageGenError(Exception):
    pass


def _call(prompt: str, aspect_ratio: str, negative_prompt: str) -> bytes:
    body = json.dumps({
        "prompt": prompt[:1500],
        "mode": "text-to-image",
        "aspect_ratio": aspect_ratio,
        "output_format": "png",
        "negative_prompt": negative_prompt,
    })
    resp = _bedrock.invoke_model(
        modelId=settings.bedrock_image_model_id,
        contentType="application/json",
        accept="application/json",
        body=body,
    )
    payload = json.loads(resp["body"].read())

    # Filter check
    finish_reasons = payload.get("finish_reasons") or []
    if finish_reasons and finish_reasons[0]:
        raise ImageGenError(f"Stability filtered output: {finish_reasons[0]}")

    images = payload.get("images") or []
    if not images:
        raise ImageGenError(f"Stability returned no images: {payload}")
    return base64.b64decode(images[0])


async def generate_background_png(
    prompt: str,
    aspect_ratio: str = "9:16",
    negative_prompt: str = "person, people, human, face, hands, text, watermark, logo, blurry, low quality, distorted, cartoon",
) -> bytes:
    """Generate a vertical 9:16 photorealistic PNG from a text prompt.

    Defaults exclude people so the avatar (composited in front later) doesn't conflict
    with figures generated into the background.
    """
    try:
        return await asyncio.to_thread(_call, prompt, aspect_ratio, negative_prompt)
    except ImageGenError:
        raise
    except Exception as e:
        raise ImageGenError(str(e)) from e
