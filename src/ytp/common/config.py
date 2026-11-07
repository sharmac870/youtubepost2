"""Settings (LLD Section 12). Non-secret values come from environment variables; secrets come from
Key Vault through `ytp.common.secrets`. Startup validates everything and fails fast.

Defaults are tuned for the lowest running cost. Each one that differs from the LLD says why.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Csv = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env.local", extra="ignore", case_sensitive=False)

    # Where state, queues and media live. "local" uses SQLite and a folder; "azure" uses Table Storage,
    # Storage Queues and Blob Storage in one storage account, reached by managed identity.
    backend: Literal["local", "azure"] = "local"
    data_dir: Path = Path(".data")
    storage_account: str = ""            # azure: account name, e.g. ytp2store
    key_vault_uri: str = ""              # azure: https://<vault>.vault.azure.net/
    azure_client_id: str = ""            # user-assigned managed identity, when used

    # Discovery
    region_code: str = "IN"
    # Sports, Gaming, Entertainment, News, Howto, Science & Tech. Music (10) is excluded: copyright risk.
    category_allowlist: Csv = ["17", "20", "24", "25", "26", "28"]
    # LLD default is 5. Two per run x 4 runs a day = 8 drafts a day, enough to pick 3 good ones and
    # keeps Claude and YouTube quota use low.
    top_n_topics: int = Field(2, ge=1, le=10)
    topic_dedupe_days: int = 7
    max_results_per_category: int = Field(25, ge=1, le=50)
    deny_channel_ids: Csv = []
    news_context: bool = True            # Google News RSS headlines as sources for factual claims
    # Of the top_n_topics per run, how many come from the news-story hierarchy (cyber, tech, business,
    # breaking) instead of the YouTube chart. 0 turns news-led stories off.
    news_topics_per_run: int = Field(1, ge=0, le=10)
    news_niches: Csv = ["cyber", "tech", "business", "viral"]
    news_lookback_hours: int = Field(36, ge=6, le=168)
    fetch_articles: bool = True          # read the top article's text for facts (best effort)

    # Channel analytics (read-only, API key): refreshed by the sweep job at most this often
    yt_channel_handle: str = "@chansharma870"
    channel_stats_every_hours: int = Field(3, ge=1, le=48)

    # Claude. Haiku is about 5x cheaper than Sonnet and writes 60-second scripts well; switch with one env var.
    anthropic_model: str = "claude-haiku-5-5"
    moderation_model: str = "claude-haiku-5-5"
    llm_effort: Literal["low", "medium", "high"] = "low"
    llm_max_tokens: int = 8000
    # USD per million tokens, for the spend cap. Check https://www.anthropic.com/pricing.
    llm_price_in_per_mtok: float = 1.0
    llm_price_out_per_mtok: float = 5.0
    daily_spend_cap_inr: float = 150.0
    usd_to_inr: float = 88.0

    # Policy and approval (LLD Section 11)
    auto_approve_categories: Csv = []
    approval_ttl_hours: int = 6
    preview_before_approval: bool = True   # render each draft's video before review; Approve reuses it
    deny_topics: Csv = [
        "election", "elections", "lok sabha", "vidhan sabha", "bjp", "congress party", "campaign rally", "exit poll",
        "communal", "riot", "mob lynching", "temple mosque", "hindu muslim", "terror attack", "blast",
        "earthquake", "flood", "floods", "stampede", "dead", "killed", "death toll", "murder", "rape", "suicide",
        "arrested", "accused", "fir", "stock tip", "multibagger", "crypto pump", "cure", "diagnosis", "minor",
    ]

    # Rendering
    tts_voice: str = "en-IN-PrabhatNeural"
    tts_rate: str = "+10%"
    language: Literal["en-IN", "hi-IN"] = "en-IN"
    render_timeout_s: int = 1200
    max_video_seconds: float = 58.0

    # Publishing
    platforms: Annotated[list[Literal["YOUTUBE", "INSTAGRAM"]], NoDecode] = ["YOUTUBE"]
    publish_kill_switch: bool = False    # the console can also flip this at run time
    yt_channel_handle: str = "@chansharma870"
    yt_privacy_status: Literal["private", "unlisted", "public"] = "private"  # weeks 1-2 of LLD 11.3
    yt_daily_quota_units: int = 10000
    yt_quota_stop_pct: int = 80
    yt_insert_cost: int = 1600           # verify with the current YouTube quota calculator
    ig_username: str = "sharmac_870"
    ig_user_id: str = ""
    # graph.instagram.com = "Instagram API with Instagram Login" (no Facebook Page needed);
    # graph.facebook.com = Facebook Login with a linked Page.
    ig_api_host: str = "graph.instagram.com"
    meta_graph_version: str = "v24.0"
    ig_upload_mode: Literal["resumable", "sas_url"] = "resumable"
    ig_publish_headroom: int = 5

    # Console
    allowed_users: Csv = []        # e-mail addresses allowed to sign in; empty = any signed-in user (local only)
    admin_users: Csv = []          # may flip the kill switch and change settings
    console_dev_auth: bool = False       # local development only: skip sign-in

    @field_validator("category_allowlist", "auto_approve_categories", "allowed_users", "admin_users",
                     "deny_channel_ids", "deny_topics", "platforms", "news_niches", mode="before")
    @classmethod
    def _split(cls, value):
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    def check(self) -> "Settings":
        if self.backend == "azure":
            missing = [name for name in ("storage_account", "key_vault_uri") if not getattr(self, name)]
            if missing:
                raise ValueError(f"backend=azure needs {', '.join(missing)}")
        if self.console_dev_auth and self.backend == "azure":
            raise ValueError("console_dev_auth must never be on in Azure")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings().check()
