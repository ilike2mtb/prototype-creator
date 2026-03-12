from pydantic import field_validator
from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    debug: bool = False

    # ── Anthropic LLM ────────────────────────────────────────────────────────
    anthropic_api_key: str = ""

    # ── Figma direct API ─────────────────────────────────────────────────────
    figma_token: str = ""

    # ── Service auth (Custom GPT uses X-Service-Key header) ──────────────────
    service_key: str = ""
    request_timeout_seconds: float = 30.0
    public_base_url: str = ""

    # ── Figma defaults (reuse existing Render env var names) ─────────────────
    # figma_file_key_2 is the primary default file used by both the Custom GPT
    # endpoints (when no file_key query param is supplied) and the LLM pipeline.
    figma_file_key: Optional[str] = None       # FIGMA_FILE_KEY   (fallback)
    figma_file_key_2: Optional[str] = None     # FIGMA_FILE_KEY_2 (primary default)
    figma_node_ids: str = ""                   # FIGMA_NODE_IDS   (default node IDs)
    figma_image_ids: str = ""                  # FIGMA_IMAGE_IDS  (default image IDs)
    figma_depth: int = 2                       # FIGMA_DEPTH
    figma_image_format: str = "png"            # FIGMA_IMAGE_FORMAT
    figma_image_scale: float = 1.0             # FIGMA_IMAGE_SCALE
    figma_image_batch_size: int = 50           # FIGMA_IMAGE_BATCH_SIZE

    # ── SharePoint / MS Graph ────────────────────────────────────────────────
    ms_tenant_id: str = ""
    ms_client_id: str = ""
    ms_client_secret: str = ""
    sp_site_id: str = ""
    sp_drive_id: str = ""
    sp_file_path: str = ""
    sp_file_path_example: str = ""
    sp_worksheet_name: str = ""

    # ── CORS (React frontend) ────────────────────────────────────────────────
    # Accepts a JSON array OR a comma-separated string, so the Render env var
    # can be set simply as: https://prototype-creator-ui.onrender.com
    # Multiple origins: https://a.onrender.com,https://b.onrender.com
    cors_origins: list[str] = ["http://localhost:5173"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v):
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            raw = v.strip()
            if raw.startswith("["):
                import json as _json
                try:
                    return _json.loads(raw)
                except Exception:
                    pass
            return [o.strip() for o in raw.split(",") if o.strip()]
        return v

    class Config:
        env_file = ".env"


settings = Settings()
