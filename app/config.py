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

    # Property Monitor AVM API (valuations + rental/yield data). Two key headers.
    pm_api_key: str = ""
    pm_company_key: str = ""
    pm_base_url: str = "https://api.propertymonitor.com"

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

    # Telegram bot
    telegram_bot_token: str = ""

    # Comma-separated list of allowed CORS origins. Default "*" for dev; tighten in prod via env.
    cors_origins: str = "*"

    # App
    log_level: str = "INFO"
    app_env: str = "development"

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
