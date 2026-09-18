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
    # Currency to assume for a SAP AR invoice when the gateway's get_invoices
    # response carries no recognizable currency field of its own.
    #
    # USD, not UZS: MGMG's SAP AR invoices are issued in dollars (confirmed by
    # the business owner directly, and corroborated by the data itself --
    # balances come through as 9764.31 / 6329.11 / 1841.43, i.e. fractional
    # amounts in the thousands. Uzbek sum invoices are neither fractional nor
    # that small; read as UZS those same figures would mean a hotel owes
    # roughly 75 cents). The gateway does not reliably send a currency field,
    # so defaulting this to UZS silently relabeled every real dollar invoice
    # as so'm across the daily brief, the receivables alert and OPS Manager
    # Bot alike. An explicit currency from SAP always wins over this -- see
    # push_handler._extract_currency.
    sap_default_currency: str = "USD"

    # --- SAP gateway push (the gateway's own machine pushes here; see
    # scripts/sap-gateway-push/ and integrations/sap/push_handler.py) ---
    sap_push_webhook_secret: SecretStr = SecretStr("")

    # --- MGMG's own sales CRM ---
    crm_base_url: str = "https://sales-crm-roan-six.vercel.app"
    crm_api_key: SecretStr = SecretStr("")

    # --- Telegram ---
    # Every scheduled agent (CEO Daily Brief, Receivables, Lead Agent) sends
    # through integrations/org_bot/notify.py via OPS_MANAGER_BOT_TELEGRAM_BOT_TOKEN
    # (below) to whoever currently holds the Director role, so the Director
    # only ever talks to two bots: OPS Manager Bot and Admin Bot.

    # --- Admin Bot (employee access approval) ---
    admin_bot_telegram_bot_token: SecretStr = SecretStr("")
    admin_bot_telegram_chat_id: str = ""  # the admin's own chat -- join-request cards land here
    admin_bot_webhook_secret: SecretStr = SecretStr("")
    # Optional hardening: if set, only this Telegram user id's Accept/Reject
    # taps are honored -- granting system access has a big blast radius, so
    # this checks the clicker, not just the button. 0 = disabled (whoever can
    # see the button is trusted).
    admin_bot_admin_user_id: int = 0

    # --- OPS Manager Bot (AI task routing) ---
    # No ops_manager_bot_telegram_chat_id -- it replies to whoever messaged
    # it, resolved per-sender via the employees table, not a fixed destination.
    ops_manager_bot_telegram_bot_token: SecretStr = SecretStr("")
    ops_manager_bot_webhook_secret: SecretStr = SecretStr("")
    # Runs on its OWN provider switch, independent of the global ai_provider
    # Lead Agent uses, so the two can be moved separately. Fallback stays on
    # the SAME provider (OpenRouterClient opens one httpx client per provider;
    # a cross-provider fallback would need a second client entirely).
    # Switched from DeepSeek to OpenRouter + Gemini 3.8 Flash on 2026-09-14,
    # verified live against this bot's real prompts first: correct routing
    # (including "kechagi reportlarni yozib ber" -> crm_agent, which DeepSeek
    # got wrong), Russian in -> Russian out, and "$" amounts kept as dollars.
    # Fallback is Gemini 3.7 Flash: same price and provider, so a 3.8 outage
    # degrades to a known-good model instead of failing the reply.
    ops_manager_bot_provider: str = "openrouter"  # "openrouter" | "deepseek"
    ops_manager_bot_model: str = "google/gemini-3.8-flash"
    ops_manager_bot_fallback_models: str = "google/gemini-3.7-flash"

    # --- Lead Agent on/off ---
    # Off unless explicitly turned on: every run spends SerpAPI, Tavily and
    # OpenRouter credits, and the business paused it on 2026-09-16. Set
    # LEAD_AGENT_ENABLED=true in Render's mgmg-shared group to resume; it takes
    # effect on the next 08:00 run.
    lead_agent_enabled: bool = False

    # --- Daily reports on/off ---
    # Off unless explicitly turned on (paused 2026-09-18, not in use yet).
    # When off, the 16:00/17:00 jobs send nothing and open no rows, and the
    # morning brief hides its "didn't report" section — otherwise it would
    # keep naming the last test day's non-reporters indefinitely. Set
    # DAILY_REPORTS_ENABLED=true in Render's mgmg-shared group to start.
    daily_reports_enabled: bool = False

    # --- Lead Agent sources ---
    serpapi_api_key: SecretStr = SecretStr("")
    tavily_api_key: SecretStr = SecretStr("")
    worldbank_search_url: str = "https://search.worldbank.org/api/v3/projects"
    # eTender UZEX / xt-xarid / data.egov.uz endpoints: added once confirmed
    # against the real n8n workflow (their sites serve an SPA shell, not JSON,
    # at any plausible guessed API path — see integrations/tenders/README.md).
    etender_uzex_url: str = ""
    xt_xarid_url: str = ""
    egov_opendata_url: str = ""

    # --- AI provider (Lead Agent qualification) ---
    # Both OpenRouter and DeepSeek's own API are OpenAI-compatible chat
    # completion endpoints, so one client handles either — this setting picks
    # which one, so switching back later is a one-line env change, not a
    # code change.
    ai_provider: str = "openrouter"  # "openrouter" | "deepseek"

    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_model: str = "google/gemini-3.8-flash"
    # Comma-separated models tried in order when the primary fails. Free
    # (":free") models share a congested pool and returned 429 on 3 of 4 local
    # test runs, so a chain ending on cheap paid capacity is what makes this
    # agent survive unattended every morning.
    openrouter_fallback_models: str = "google/gemini-3.7-flash"

    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_fallback_models: str = "deepseek-v4-flash,deepseek-v4-pro"

    # --- Google Sheets (Lead Agent storage) ---
    google_service_account_json: SecretStr = SecretStr("")  # full JSON key, not a file path
    google_leads_sheet_id: str = ""

    # --- Agent write gate (security rule #1) ---
    agent_writes_enabled: bool = False

    # --- Bot kill switch ---
    # One flag, checked at every entry point that can send or receive on
    # Admin Bot / OPS Manager Bot's tokens: both webhook routes (incoming
    # Telegram updates) and notify_directors() (the scheduled agents' outgoing
    # sends -- CEO Daily Brief, Receivables, Lead Agent all route through
    # OPS Manager Bot's token, so this also silences those). Flip back to
    # false and restart to resume -- nothing is deleted or reconfigured.
    bots_frozen: bool = False

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
