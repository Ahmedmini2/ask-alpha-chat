"""Tests for the personal-branding image feature: the template catalog + prompt builder (pure),
the Gemini error→message mapping, and the generate_branding_image handler flow (fully mocked —
no network, no DB, no S3, no Gemini)."""
import uuid

import pytest

import app.tools.branding as branding
from app.branding import templates as tmpl
from app.integrations.gemini_images import GeminiImageError

VALID_RATIOS = {"1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"}


# --------------------------------- catalog (pure) ---------------------------------

def test_catalog_has_twelve_unique_templates_with_bundled_files():
    ts = tmpl.all_templates()
    assert len(ts) == 12
    slugs = [t.slug for t in ts]
    assert len(set(slugs)) == 12, "slugs must be unique"
    for t in ts:
        assert t.path.exists(), f"bundled file missing for {t.slug}"
        assert t.aspect_ratio in VALID_RATIOS, f"{t.slug} has bad ratio {t.aspect_ratio}"
        assert t.title and t.description and t.suggested_text
        assert t.scene_description and t.text_treatment and t.subject_direction


def test_get_template_is_case_insensitive_and_safe():
    assert tmpl.get_template("NO-DAYS-OFF").slug == "no-days-off"
    assert tmpl.get_template("  busy-selling ").slug == "busy-selling"
    assert tmpl.get_template("does-not-exist") is None
    assert tmpl.get_template(None) is None
    assert tmpl.get_template("") is None


def test_build_prompt_with_text_includes_overlay_and_identity_lock():
    t = tmpl.get_template("no-days-off")
    p = tmpl.build_prompt(t, "No days off")
    assert '"No days off"' in p
    assert "IMAGE 1" in p and "IMAGE 2" in p
    assert "MOST IMPORTANT" in p  # identity preservation emphasis
    assert t.aspect_ratio in p
    assert "do NOT render" not in p  # text variant, not the clean one


def test_build_prompt_clean_variant_has_no_text():
    t = tmpl.get_template("busy-selling")
    p = tmpl.build_prompt(t, None)
    assert "do NOT render any headline" in p
    assert t.clean_variant_note in p
    p_empty = tmpl.build_prompt(t, "   ")
    assert "do NOT render any headline" in p_empty  # whitespace == no text


# --------------------------------- error mapping ---------------------------------

def test_gemini_error_messages_by_kind():
    assert "billing" in branding._gemini_error_message(GeminiImageError("x", kind="quota")).lower()
    assert "admin" in branding._gemini_error_message(GeminiImageError("x", kind="config")).lower()
    assert "safety" in branding._gemini_error_message(GeminiImageError("x", kind="blocked")).lower()
    assert branding._gemini_error_message(GeminiImageError("x", kind="api"))


# --------------------------------- handler (mocked) ---------------------------------

class FakeProfile:
    def __init__(self, role="salesagent", ask_alpha_access="write",
                 avatar_key="profile-pics/abc/avatar.jpg"):
        self.role = role
        self.ask_alpha_access = ask_alpha_access
        self.avatar_key = avatar_key


def _ctx(telegram=None):
    return {"user_id": uuid.uuid4(), "telegram_chat_id": telegram}


@pytest.fixture
def agent_env(monkeypatch):
    """Patch the handler's collaborators so it runs offline as a valid agent."""
    monkeypatch.setattr(branding, "get_profile", _aret(FakeProfile()))
    monkeypatch.setattr(branding.settings, "branding_images_enabled", True)
    monkeypatch.setattr(branding.gemini_images, "configured", lambda: True)
    monkeypatch.setattr(branding, "_persist", _aret(None))
    monkeypatch.setattr(branding, "_build_thumbnails", _aret({t.slug: None for t in tmpl.all_templates()}))
    return monkeypatch


def _aret(value):
    async def _f(*a, **k):
        return value
    return _f


@pytest.mark.asyncio
async def test_anonymous_rejected(agent_env, monkeypatch):
    monkeypatch.setattr(branding, "get_profile", _aret(None))
    out = await branding.generate_branding_image_handler(None, {"action": "list_templates"},
                                                         {"user_id": None})
    assert "error" in out and "sign in" in out["error"].lower()


@pytest.mark.asyncio
async def test_non_agent_rejected(agent_env, monkeypatch):
    monkeypatch.setattr(branding, "get_profile", _aret(FakeProfile(role="buyer")))
    out = await branding.generate_branding_image_handler(None, {"action": "list_templates"}, _ctx())
    assert "error" in out and "agents" in out["error"].lower()


@pytest.mark.asyncio
async def test_list_templates_returns_twelve(agent_env):
    out = await branding.generate_branding_image_handler(None, {"action": "list_templates"}, _ctx())
    assert out["status"] == "templates"
    assert out["count"] == 12
    assert {t["id"] for t in out["templates"]} == {t.slug for t in tmpl.all_templates()}


@pytest.mark.asyncio
async def test_generate_unknown_template_relists(agent_env):
    out = await branding.generate_branding_image_handler(
        None, {"action": "generate", "template_id": "nope"}, _ctx())
    assert out["status"] == "needs_template"
    assert out["templates"]


@pytest.mark.asyncio
async def test_generate_wants_text_but_none_given(agent_env):
    out = await branding.generate_branding_image_handler(
        None, {"action": "generate", "template_id": "no-days-off", "add_text": True}, _ctx())
    assert out["status"] == "needs_text"
    assert out["template_id"] == "no-days-off"
    assert out["suggested_text"]


@pytest.mark.asyncio
async def test_generate_text_too_long(agent_env):
    long_text = "x" * (tmpl.MAX_OVERLAY_CHARS + 1)
    out = await branding.generate_branding_image_handler(
        None, {"action": "generate", "template_id": "no-days-off", "overlay_text": long_text}, _ctx())
    assert out["status"] == "text_too_long"
    assert out["max_chars"] == tmpl.MAX_OVERLAY_CHARS


@pytest.mark.asyncio
async def test_generate_no_avatar(agent_env, monkeypatch):
    monkeypatch.setattr(branding, "get_profile", _aret(FakeProfile(avatar_key=None)))
    out = await branding.generate_branding_image_handler(
        None, {"action": "generate", "template_id": "no-days-off", "add_text": False}, _ctx())
    assert "error" in out and "profile picture" in out["error"].lower()


@pytest.mark.asyncio
async def test_generate_happy_path_clean(agent_env, monkeypatch):
    monkeypatch.setattr(branding.brochure_storage, "fetch_asset_bytes", _aret(b"JPEGDATA"))
    captured = {}

    async def fake_gen(template_bytes, profile_bytes, prompt, **kw):
        captured["prompt"] = prompt
        captured["aspect_ratio"] = kw.get("aspect_ratio")
        return b"PNGBYTES"

    monkeypatch.setattr(branding.gemini_images, "generate_branding_image", fake_gen)
    monkeypatch.setattr(branding.brochure_storage, "upload_png", _aret(("k/x.png", "https://signed/x")))
    monkeypatch.setattr(branding, "_send_telegram_photo", _aret(False))

    out = await branding.generate_branding_image_handler(
        None, {"action": "generate", "template_id": "no-days-off", "add_text": False}, _ctx())
    assert out["status"] == "completed"
    assert out["has_text"] is False
    assert out["overlay_text"] is None
    assert out["image_url"] == "https://signed/x"
    assert captured["aspect_ratio"] == tmpl.get_template("no-days-off").aspect_ratio
    assert "do NOT render any headline" in captured["prompt"]


@pytest.mark.asyncio
async def test_generate_happy_path_with_text_and_telegram(agent_env, monkeypatch):
    monkeypatch.setattr(branding.brochure_storage, "fetch_asset_bytes", _aret(b"JPEGDATA"))
    monkeypatch.setattr(branding.gemini_images, "generate_branding_image", _aret(b"PNGBYTES"))
    monkeypatch.setattr(branding.brochure_storage, "upload_png", _aret(("k/x.png", "https://signed/x")))
    sent = {}
    async def fake_tg(chat_id, png, filename, caption):
        sent["chat_id"] = chat_id
        return True
    monkeypatch.setattr(branding, "_send_telegram_photo", fake_tg)

    out = await branding.generate_branding_image_handler(
        None, {"action": "generate", "template_id": "busy-selling", "overlay_text": "Busy selling"},
        _ctx(telegram=999))
    assert out["status"] == "completed"
    assert out["has_text"] is True
    assert out["overlay_text"] == "Busy selling"
    assert out["sent_to_telegram"] is True
    assert sent["chat_id"] == 999


@pytest.mark.asyncio
async def test_generate_quota_error_surfaces_billing(agent_env, monkeypatch):
    monkeypatch.setattr(branding.brochure_storage, "fetch_asset_bytes", _aret(b"JPEGDATA"))

    async def boom(*a, **k):
        raise GeminiImageError("quota", kind="quota")

    monkeypatch.setattr(branding.gemini_images, "generate_branding_image", boom)
    out = await branding.generate_branding_image_handler(
        None, {"action": "generate", "template_id": "no-days-off", "add_text": False}, _ctx())
    assert "error" in out and "billing" in out["error"].lower()


@pytest.mark.asyncio
async def test_generate_delivery_failure_errors(agent_env, monkeypatch):
    monkeypatch.setattr(branding.brochure_storage, "fetch_asset_bytes", _aret(b"JPEGDATA"))
    monkeypatch.setattr(branding.gemini_images, "generate_branding_image", _aret(b"PNGBYTES"))
    async def upload_fail(*a, **k):
        raise RuntimeError("s3 down")
    monkeypatch.setattr(branding.brochure_storage, "upload_png", upload_fail)
    monkeypatch.setattr(branding, "_send_telegram_photo", _aret(False))
    out = await branding.generate_branding_image_handler(
        None, {"action": "generate", "template_id": "no-days-off", "add_text": False}, _ctx())
    assert "error" in out and "couldn't be delivered" in out["error"].lower()
