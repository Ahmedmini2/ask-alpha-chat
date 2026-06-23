"""Personal-branding template catalog (app/branding/templates).

Each template is a curated editorial poster style. At generation time we hand Nano Banana Pro
the bundled template JPEG (style reference) + the agent's profile photo, and build a prompt from
these fields so the agent is restyled into the template (their face preserved), with an optional
headline overlay. Bundled JPEGs live next to this file under templates/; the per-template prompt
fields live in template_data.json (also bundled).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_DIR = Path(__file__).parent
TEMPLATE_DIR = _DIR / "templates"
S3_KEY_PREFIX = "branding/templates"
MAX_OVERLAY_CHARS = 60


@dataclass(frozen=True)
class BrandingTemplate:
    slug: str
    title: str
    description: str          # one-line, shown to the agent when picking
    suggested_text: str       # a short headline we can offer if they want text
    aspect_ratio: str         # Nano Banana Pro aspect ratio (e.g. "4:5", "2:3", "9:16")
    file: str                 # bundled JPEG filename under templates/
    scene_description: str
    text_treatment: str
    clean_variant_note: str
    subject_direction: str

    @property
    def path(self) -> Path:
        return TEMPLATE_DIR / self.file

    @property
    def s3_key(self) -> str:
        return f"{S3_KEY_PREFIX}/{self.file}"

    def read_bytes(self) -> bytes:
        return self.path.read_bytes()


def _load() -> list[BrandingTemplate]:
    raw = json.loads((_DIR / "template_data.json").read_text(encoding="utf-8"))
    return [BrandingTemplate(**t) for t in raw]


TEMPLATES: list[BrandingTemplate] = _load()
_BY_SLUG = {t.slug: t for t in TEMPLATES}


def all_templates() -> list[BrandingTemplate]:
    return list(TEMPLATES)


def get_template(slug: str | None) -> BrandingTemplate | None:
    if not slug:
        return None
    return _BY_SLUG.get(slug.strip().lower())


def build_prompt(template: BrandingTemplate, overlay_text: str | None) -> str:
    """Assemble the Nano Banana Pro instruction. IMAGE 1 = template (style ref), IMAGE 2 = the
    agent's profile photo. overlay_text=None/"" -> the clean, text-free variant."""
    overlay = (overlay_text or "").strip()
    if overlay:
        text_block = (
            f'TEXT — render this exact headline, spelled letter-for-letter: "{overlay}". '
            f"{template.text_treatment} The headline text must be sharp, legible and correctly "
            "spelled. Do not add any other words, watermark or logo."
        )
    else:
        text_block = (
            "TEXT — do NOT render any headline, slogan, caption, watermark or lettering anywhere "
            f"in the image. {template.clean_variant_note}"
        )
    return (
        "You are given TWO images. IMAGE 1 is a STYLE/TEMPLATE reference for a real-estate agent "
        "personal-branding poster. IMAGE 2 is a REAL PERSON (a real-estate agent).\n\n"
        "Create ONE new photorealistic vertical poster in the EXACT visual style of IMAGE 1, but "
        "the person in it MUST be the person from IMAGE 2.\n\n"
        f"SCENE (reproduce faithfully from IMAGE 1): {template.scene_description}\n\n"
        f"SUBJECT POSE & STYLING: {template.subject_direction}\n\n"
        "IDENTITY — THIS IS THE MOST IMPORTANT REQUIREMENT: the subject must be unmistakably the "
        "SAME person as in IMAGE 2. Preserve their exact face, facial features, skin tone, hair and "
        "apparent age and gender. Do NOT copy the face, body or gender of the person in IMAGE 1 — "
        "take ONLY their pose, wardrobe, framing, lighting and styling.\n\n"
        f"{text_block}\n\n"
        f"Output a single high-resolution {template.aspect_ratio} portrait image with a premium, "
        "polished, editorial real-estate personal-branding aesthetic."
    )
