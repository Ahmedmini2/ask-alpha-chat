"""Classify project photos with the Bedrock vision model so the brochure can
place them sensibly (cover hero vs interiors gallery vs amenities columns) and
caption them in the template's short uppercase style.

One converse() call classifies up to ~12 images. On any failure we fall back to
generic position-based captions — the brochure must never fail because of this.
"""
import asyncio
import json
import logging

import boto3

from app.config import settings

log = logging.getLogger("askalpha.brochures")

_bedrock = boto3.client("bedrock-runtime", region_name=settings.aws_region)

# Categories the layout logic understands.
CATEGORIES = [
    "building_exterior", "living_room", "kitchen", "bedroom", "bathroom",
    "balcony_terrace", "pool", "garden", "gym", "lobby", "kids_area",
    "spa", "amenity_other", "floor_plan", "map_or_diagram", "other",
]

_PROMPT = """You will see {n} numbered real-estate photos of one Dubai development.
For EACH photo, classify it and write a short elegant caption (1-3 words, e.g.
"Living Room", "Lagoon Pool", "Master Bedroom", "Gardens & Pool").

Category must be exactly one of:
{cats}

Reply with ONLY a JSON array, one object per photo in the same order:
[{{"i": 0, "category": "...", "caption": "..."}}, ...]"""

_MAX_IMAGE_BYTES = 4_500_000  # Bedrock converse caps images at ~5MB


def _classify_sync(images: list[bytes]) -> list[dict]:
    content: list[dict] = []
    for img in images:
        content.append({"image": {"format": "jpeg", "source": {"bytes": img}}})
    content.append({"text": _PROMPT.format(n=len(images), cats=", ".join(CATEGORIES))})
    resp = _bedrock.converse(
        modelId=settings.bedrock_model_id,
        messages=[{"role": "user", "content": content}],
        inferenceConfig={"maxTokens": 1200, "temperature": 0},
    )
    text = resp["output"]["message"]["content"][0]["text"].strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON array in vision reply: {text[:200]}")
    return json.loads(text[start : end + 1])


async def classify_photos(images: list[bytes]) -> list[dict]:
    """Returns [{category, caption}, ...] aligned with `images`.

    Oversized or unclassifiable images get {"category": "other", "caption": ""}.
    """
    fallback = [{"category": "other", "caption": ""} for _ in images]
    usable_idx = [i for i, b in enumerate(images) if b and len(b) <= _MAX_IMAGE_BYTES]
    if not usable_idx:
        return fallback
    try:
        raw = await asyncio.to_thread(_classify_sync, [images[i] for i in usable_idx])
    except Exception as e:
        log.warning("photo classification failed (%s) — using generic captions", e)
        return fallback

    out = list(fallback)
    for pos, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        # Trust the model's self-reported index when sane — with many images its
        # array order can drift, which mislabels every photo after the slip.
        i = item.get("i")
        slot = i if isinstance(i, int) and 0 <= i < len(usable_idx) else pos
        if slot >= len(usable_idx):
            continue
        cat = str(item.get("category", "other"))
        if cat not in CATEGORIES:
            cat = "other"
        caption = str(item.get("caption", "")).strip()[:40]
        out[usable_idx[slot]] = {"category": cat, "caption": caption}
    return out
