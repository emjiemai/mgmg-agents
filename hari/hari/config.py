"""Settings from environment / .env. Secrets never appear in code or logs."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

TASHKENT = ZoneInfo("Asia/Tashkent")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: SecretStr = SecretStr("")
    database_url: str = "sqlite+aiosqlite:///./hari.db"

    openrouter_api_key: SecretStr = SecretStr("")
    llm_model: str = "google/gemini-3.8-flash"
    llm_fallback_models: str = "google/gemini-3.7-flash"
    llm_timeout_seconds: float = 40.0

    knowledge_dir: str = "knowledge/samples"
    proactive_daily_limit: int = 2
    hari_paused: bool = False

    # Work hours used for proactive messages when an employee has no schedule (spec §10, A07)
    default_work_start_hour: int = 9
    default_work_end_hour: int = 18
    # First answer target for an HR case, in work hours (spec §10 escalation)
    case_first_response_hours: int = 4
    # One-time invite link lifetime
    invite_ttl_hours: int = 72

    @property
    def llm_models(self) -> list[str]:
        extra = [m.strip() for m in self.llm_fallback_models.split(",") if m.strip()]
        return [self.llm_model, *extra]



settings = Settings()
