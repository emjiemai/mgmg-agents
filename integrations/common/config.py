"""Central configuration, loaded once from .env.

Every credential in the project comes through this module — nothing is hardcoded
(security rule #3). Import ``settings`` and read attributes off it.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """All runtime configuration for the Command Center.

    Values come from the process environment, falling back to ``.env`` at the
    project root. Secrets are wrapped in ``SecretStr`` so they never leak into
    logs or tracebacks; call ``.get_secret_value()`` at the point of use.

    Raises:
        pydantic.ValidationError: if a required variable is missing entirely.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- General ---
    tz: str = "Asia/Tashkent"
    environment: str = "production"
    log_level: str = "INFO"
    dry_run: bool = False

    # --- PostgreSQL ---
    # Render (and most managed-Postgres hosts) inject one connection string
    # instead of discrete fields. When DATABASE_URL is set it wins outright;
    # the postgres_* fields below are only used for local/VPS setups.
    database_url: str = ""
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "mgmg"
    postgres_user: str = ""
    postgres_password: SecretStr = SecretStr("")

    # --- SAP Business One Service Layer ---
    sap_base_url: str = ""
    sap_company_db: str = ""
    sap_username: str = ""
    sap_password: SecretStr = SecretStr("")
    sap_verify_ssl: bool = False
    sap_timeout_seconds: int = 60

    # --- MGMG's own sales CRM (replaces amoCRM) ---
    crm_base_url: str = "https://sales-crm-roan-six.vercel.app"
    crm_api_key: SecretStr = SecretStr("")

    # --- amoCRM / Kommo (superseded by the in-house CRM above; kept only
    # for the parts of the codebase not yet migrated off it) ---
    amocrm_subdomain: str = ""
    amocrm_domain: str = "amocrm.ru"
    amocrm_long_lived_token: SecretStr = SecretStr("")
    amocrm_webhook_secret: SecretStr = SecretStr("")
    amocrm_max_rps: float = 7.0

    # --- Microsoft Graph (app-only) ---
    ms_tenant_id: str = ""
    ms_client_id: str = ""
    ms_client_secret: SecretStr = SecretStr("")
    ms_graph_scope: str = "https://graph.microsoft.com/.default"
    ms_team_owner_upn: str = ""
    ms_planner_group_id: str = ""
    ms_planner_default_plan_id: str = ""
    teams_sales_webhook_url: str = ""

    # --- Telegram ---
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_ceo_chat_id: str = ""
    telegram_alerts_chat_id: str = ""

    # --- Verifix ---
    verifix_mode: str = "csv"
    verifix_base_url: str = ""
    verifix_api_token: SecretStr = SecretStr("")
    verifix_csv_dir: str = str(PROJECT_ROOT / "data" / "verifix")

    # --- Agent write gate (security rule #1) ---
    agent_writes_enabled: bool = False

    # --- Business rules ---
    default_currency: str = "UZS"
    usd_uzs_reference_rate: int = 12800
    ar_bucket_critical_days: int = 90
    ar_bucket_warning_days: int = 30
    daily_brief_hour_local: int = 8

    # --- Derived ---
    @property
    def postgres_dsn(self) -> str:
        """libpq connection string for psycopg.

        Prefers ``DATABASE_URL`` (Render's convention: ``postgres://user:pass@host/db``,
        possibly with ``?sslmode=require``) when set, since psycopg accepts a
        URL directly. Falls back to discrete fields for local/VPS setups.
        """
        if self.database_url:
            return self.database_url
        return (
            f"host={self.postgres_host} port={self.postgres_port} "
            f"dbname={self.postgres_db} user={self.postgres_user} "
            f"password={self.postgres_password.get_secret_value()}"
        )

    @property
    def amocrm_base_url(self) -> str:
        """Root URL of the amoCRM/Kommo account, e.g. https://acme.amocrm.ru."""
        return f"https://{self.amocrm_subdomain}.{self.amocrm_domain}"

    def missing_placeholders(self) -> list[str]:
        """Return names of settings still holding a ``[PLACEHOLDER]`` value.

        Used by every entry point to refuse to run against live systems before
        the operator has filled in real credentials.

        Returns:
            Field names whose value still looks like an unfilled placeholder.
        """
        unfilled: list[str] = []
        for name in self.model_fields:
            raw = getattr(self, name)
            value = raw.get_secret_value() if isinstance(raw, SecretStr) else raw
            if isinstance(value, str) and value.startswith("[") and value.endswith("]"):
                unfilled.append(name)
        return unfilled


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once per process.

    Returns:
        The cached ``Settings`` instance.

    Raises:
        pydantic.ValidationError: on malformed values (e.g. non-integer port).
    """
    return Settings()


settings = get_settings()
