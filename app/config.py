from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # AWS / Bedrock
    aws_region: str = "us-east-1"
    # Main reasoning/tool-use model. To upgrade to Claude on Bedrock, set
    # BEDROCK_MODEL_ID in the env to a Claude inference profile available in your
    # region, e.g. "us.anthropic.claude-sonnet-4-5-20250929-v1:0" (reasoning) — the
    # Converse tool-loop already speaks the right API, so it's a drop-in swap.
    bedrock_model_id: str = "us.meta.llama3-3-70b-instruct-v1:0"
    # Optional cheaper model for routing/classification/grading (e.g. Claude Haiku).
    # Falls back to bedrock_model_id when unset.
    bedrock_routing_model_id: str = ""
    bedrock_embed_model_id: str = "amazon.titan-embed-text-v2:0"
    # Bedrock prompt caching for the static system prompt + tool config. Only enable
    # once on a model that supports it (Claude on Bedrock). Safe to leave off on Llama.
    enable_prompt_caching: bool = False
    # S3 assets bucket lives in eu-west-2; Textract async API requires
    # the client to be in the same region as the bucket.
    s3_assets_region: str = "eu-west-2"

    # us-west-2 is where Stability AI's text-to-image models are active on Bedrock.
    bedrock_image_region: str = "us-west-2"
    bedrock_image_model_id: str = "stability.stable-image-core-v1:1"

    # Google Gemini "Nano Banana Pro" image model — personal-branding image generation
    # (restyle the agent's profile photo to match a chosen template + render an overlay).
    # Key is GOOGLE_AI_STUDIO_API_KEY (AI Studio / Generative Language API, NOT Vertex).
    # IMPORTANT: the Gemini *image* models have NO free tier — the Google project behind the
    # key must have BILLING ENABLED, or calls fail 429 RESOURCE_EXHAUSTED (free-tier limit: 0).
    google_ai_studio_api_key: str = ""
    gemini_image_model_id: str = "gemini-3-pro-image-preview"   # Nano Banana Pro (primary)
    # Fallback when the primary is overloaded/unavailable (the pro *preview* often returns
    # 503 'high demand' under load). The flash image model is more available under load.
    # Set to "" to disable the fallback. Override via GEMINI_IMAGE_FALLBACK_MODEL_ID.
    gemini_image_fallback_model_id: str = "gemini-3.1-flash-image"
    gemini_image_size: str = "2K"                               # "1K" | "2K" | "4K" (uppercase K)
    gemini_image_timeout_sec: int = 120
    branding_images_enabled: bool = True

    # Property Monitor AVM API (valuations + rental/yield data). Two key headers.
    pm_api_key: str = ""
    pm_company_key: str = ""
    pm_base_url: str = "https://api.propertymonitor.com"
    pm_emirate_id: str = "4"            # Dubai
    # Representative unit used for a community's pre-flight AVM when ingesting.
    pm_default_property_type_id: int = 1   # 1 = Apartment
    pm_default_bedrooms: str = "2"
    pm_default_size_sqft: float = 1100.0
    pm_ingest_concurrency: int = 3      # PM is rate-limited; keep low

    # Alpha Verdict (ported from the website; numbers come from PM community stats, static fallback).
    alpha_verdict_formula_version: str = "v1"
    alpha_verdict_max_age_days: int = 7
    alpha_verdict_intent: str = "yield"   # the website's quickVerdict intent

    # Chat memory window. The model (Claude on Bedrock) has a ~200K-token window, so we can keep
    # far more than the old 10-message cap; we send up to N recent messages bounded by a character
    # budget so a few huge pastes can't blow the context. Raise either to give Alpha a longer memory.
    chat_history_max_messages: int = 60        # up to this many recent messages (≈30 turns)
    chat_history_char_budget: int = 60_000     # ~15K tokens; trims the oldest beyond this

    # HeyGen (AI avatar video generation)
    heygen_api_key: str = ""
    heygen_avatar_id: str = "Daisy-inskirt-20220818"   # HeyGen's default sample avatar
    heygen_voice_id: str = "2d5b0e6cf36f460aa7fc47e3eee4ba54"  # default English voice
    # Pin each agent to a SPECIFIC HeyGen voice_id so the avatar's voice can't drift to a
    # same-named preset (e.g. agent "Said" matching a stock voice called "Said" instead of
    # his clone). JSON map of agent name (any case) -> HeyGen voice_id, e.g.
    # HEYGEN_AGENT_VOICES='{"Said":"<voice_id>","Zain Ul Abdeen":"<voice_id>"}'. Matched on
    # the full name first, then the first token. Find a voice_id from GET /v2/voices.
    heygen_agent_voices: str = "{}"
    # Vertical 1080x1920 — Reels / TikTok native.
    heygen_video_width: int = 1080
    heygen_video_height: int = 1920

    # Descript API (optional caption post-step). When a token is set, the poller sends each
    # finished HeyGen video through Descript: import -> Underlord agent adds captions ->
    # publish -> deliver the captioned MP4. If unset, videos ship uncaptioned. Token from
    # Descript -> API (https://docs.descriptapi.com). Best-effort: any failure falls back to
    # the raw HeyGen video. NOTE: the agent controls caption STYLE (no Hormozi template
    # selector in the API) — tune the wording here to get as close as Descript allows.
    descript_api_token: str = ""
    descript_caption_prompt: str = (
        "Add bold, animated, word-by-word karaoke-style captions to the entire video — "
        "large, centered, in the lower third, one or two words highlighted at a time. "
        "Keep captions short: never show more than 2 lines on screen at once, and at most "
        "4 words per line — break the text into more, shorter caption segments rather than "
        "long lines. Do not change anything else about the video."
    )
    descript_caption_resolution: str = "1080p"
    descript_caption_concurrency: int = 1  # Descript jobs are heavy; default one at a time
    # Publish access level: the drive permits public / unlisted / private (NOT "drive").
    # "private" still yields a signed, time-limited download_url we deliver via Telegram,
    # without creating a public share page.
    descript_caption_access_level: str = "private"

    # B-roll post-edit (ffmpeg): after HeyGen renders, cut full-screen Ken-Burns stills of the
    # property into the middle of the video (avatar stays for hook + CTA), narration continuous.
    # Best-effort — any failure falls back to the raw HeyGen video. Runs BEFORE Descript captions
    # so the captions overlay the final composite. Requires ffmpeg in the image (see Dockerfile).
    broll_enabled: bool = True
    broll_max_clips: int = 5            # cap on b-roll stills cut in per video
    broll_concurrency: int = 1          # ffmpeg is CPU-heavy; one encode at a time
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    broll_ffmpeg_timeout_sec: int = 300
    broll_head_ratio: float = 0.25      # fraction of duration the avatar holds at the start (hook)
    broll_tail_ratio: float = 0.25      # fraction the avatar holds at the end (CTA)
    broll_head_min_sec: float = 3.0
    broll_head_max_sec: float = 12.0
    broll_tail_min_sec: float = 3.0
    broll_tail_max_sec: float = 10.0
    broll_min_total_dur_sec: float = 12.0   # shorter videos stay all-avatar (no b-roll)
    broll_target_segment_sec: float = 4.0
    broll_min_segment_sec: float = 2.5
    broll_max_segment_sec: float = 7.0
    broll_crf: int = 20
    broll_preset: str = "veryfast"
    broll_zoom_max: float = 1.18

    # Hormozi-style burned-in captions (FAL whisper for word timings + ffmpeg/libass). Replaces
    # the Descript caption step. Best-effort — on any failure the video ships uncaptioned.
    fal_key: str = ""
    captions_enabled: bool = True
    caption_words_per_line: int = 3
    caption_active_color: str = "#FFD60A"     # the highlighted (spoken) word
    caption_pop: bool = True                  # spring-ish scale pop on the active word
    caption_font_name: str = "Anton"          # libass Fontname (must match the bundled font family)
    caption_font_dir: str = "app/videos/assets/fonts"  # dir libass scans for the font
    fal_whisper_timeout_sec: int = 240

    # Telegram bot
    telegram_bot_token: str = ""

    # Supabase project URL, e.g. https://<ref>.supabase.co. When SET, /v1/chat authenticates
    # the caller: it verifies the Supabase access token (Authorization: Bearer …) against the
    # project's public JWKS and derives user_id from the token's `sub` — a client-supplied body
    # user_id is then NOT trusted on its own (it must match the token, or the request is treated
    # as anonymous). When UNSET, auth is disabled and the legacy body user_id is trusted (dev /
    # pre-rollout). See app/core/auth.py.
    supabase_url: str = ""

    # Ayrshare — publish to a user's connected social accounts from chat (publish_to_social).
    # This is the ONE shared "Primary Profile" API key (same value the web app uses); each
    # user's per-account Profile-Key is read straight from public.ayrshare_profiles on our
    # Postgres connection (the `postgres` role has BYPASSRLS, so no Supabase service-role REST
    # call is needed). Unset → the tool returns a "not configured" message instead of posting.
    ayrshare_api_key: str = ""

    # Comma-separated list of allowed CORS origins. Default "*" for dev; tighten in prod via env.
    cors_origins: str = "*"

    # App
    log_level: str = "INFO"
    app_env: str = "development"

    # Run the in-process HeyGen poller in THIS process. Keep True for a single combined
    # process (local / Railway). On ECS the API runs with this False — so N autoscaled API
    # tasks don't each poll — and the poller runs in ONE dedicated worker task instead. The
    # poller MUST stay a singleton: multiple instances duplicate Descript/caption jobs and
    # race the videos table. The Telegram bot process never imports app.main, so it's unaffected.
    run_heygen_poller: bool = True

    # Database (components, not full URL — avoids encoding issues)
    db_host: str = ""
    db_port: int = 5432
    db_user: str = ""
    db_password: str = ""
    db_name: str = "postgres"

    @property
    def agent_voice_map(self) -> dict:
        """Parsed heygen_agent_voices: {normalized agent name -> voice_id}. Empty on bad JSON."""
        import json
        try:
            raw = json.loads(self.heygen_agent_voices or "{}")
        except (ValueError, TypeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {" ".join(str(k).split()).lower(): str(v) for k, v in raw.items() if v}

    @property
    def database_url(self) -> URL:
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )

settings = Settings()
