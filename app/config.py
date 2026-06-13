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
    # Vertical 1080x1920 — Reels / TikTok native.
    heygen_video_width: int = 1080
    heygen_video_height: int = 1920

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
