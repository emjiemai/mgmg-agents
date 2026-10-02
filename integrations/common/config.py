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

    # --- Public address of mgmg-api, for links printed on QR codes ---
    # Set PUBLIC_BASE_URL to the address clients should see (ideally a domain
    # of the company's own, so printed codes survive a change of hosting).
    # Render sets RENDER_EXTERNAL_URL on web services; it's the fallback.
    public_base_url: str = ""
    render_external_url: str = ""

    # --- Client feedback page at /f (QR codes) ---
    feedback_enabled: bool = True

    # --- Read-only database viewer at /db (integrations/api/db_viewer.py) ---
    # Empty = the page doesn't exist (404). Set a long random password in
    # Render's mgmg-shared group to switch it on; log in with any name.
    db_viewer_password: SecretStr = SecretStr("")

    # --- Verifix (face-ID attendance, A4; integrations/verifix/) ---
    # Read-only. Client id + secret come from Verifix: Администрирование ->
    # Настройки -> Внешние системы -> Клиенты OAuth2 для сервера для компании
    # (client credentials, a role with read access to the attendance report
    # and time kinds). Empty = attendance stays out of the brief and the bot.
    # The address is fixed in integrations/verifix/client.py on purpose: old
    # VERIFIX_BASE_URL values from the removed 2026-09 integration may still
    # sit in env groups and must not redirect the credentials anywhere else.
    verifix_enabled: bool = True
    verifix_client_id: str = ""
    verifix_client_secret: SecretStr = SecretStr("")
    # The other way in (Verifix docs: "Basic auth", deprecated but working):
    # a Verifix user's login as "user@company" and password. Then
    # VERIFIX_FILIAL_ID — the organisation's ID — is required. Used only when
    # the client id/secret above are empty.
    verifix_login: str = ""
    verifix_password: SecretStr = SecretStr("")
    # The organisation's ID: required with VERIFIX_LOGIN/PASSWORD; with client
    # credentials only if Verifix says so (the client already names the organisation).
    verifix_filial_id: str = ""
    # Arriving up to this many minutes after the schedule's start is on time.
    verifix_late_grace_minutes: int = 5

    # --- 1C on Clobus (OData, read-only; integrations/onec/) ---
    # The database's OData address (https://clobus.uz/a/acc313/61458/odata/standard.odata/)
    # and the OData service user made in 1C: Администрирование → Синхронизация
    # данных → Настройки стандартного интерфейса OData → Авторизация.
    onec_odata_url: str = ""
    onec_login: str = ""
    onec_password: SecretStr = SecretStr("")

    # --- BILLZ 2.0 (shop tills; integrations/billz/) ---
    # The integration key from BILLZ: Настройки → Компания → Ключи интеграции.
    # Read-only use: shop sales in the brief and the Director's answers.
    billz_enabled: bool = True
    billz_secret_token: SecretStr = SecretStr("")

    # --- Garmin AI bot leads (POST /webhooks/garmin-lead/{secret}) ---
    # The same long random value goes into the Garmin bot's COMMAND_CENTER_SECRET.
    garmin_leads_secret: SecretStr = SecretStr("")

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
    # Its own model chain on OpenRouter, independent of OPENROUTER_MODEL (which
    # the Lead Agent uses), so the two can be moved separately.
    # Switched from DeepSeek to OpenRouter + Gemini 3.8 Flash on 2026-09-14,
    # verified live against this bot's real prompts first: correct routing
    # (including "kechagi reportlarni yozib ber" -> crm_agent, which DeepSeek
    # got wrong), Russian in -> Russian out, and "$" amounts kept as dollars.
    # Fallback is Gemini 3.7 Flash: same price and provider, so a 3.8 outage
    # degrades to a known-good model instead of failing the reply.
    ops_manager_bot_model: str = "google/gemini-3.8-flash"
    ops_manager_bot_fallback_models: str = "google/gemini-3.7-flash"

    # --- Lead Agent on/off ---
    # Off unless explicitly turned on: every run spends SerpAPI, Tavily and
    # OpenRouter credits. Paused 2026-09-16, resumed 2026-09-30 — production
    # has LEAD_AGENT_ENABLED=true in Render's mgmg-shared group (the dashboard
    # owns it); false pauses it from the next 08:00 run.
    lead_agent_enabled: bool = False

    # --- Daily reports on/off ---
    # Off unless explicitly turned on (paused 2026-09-18, not in use yet).
    # When off, the 16:00/17:00 jobs send nothing and open no rows, and the
    # morning brief hides its "didn't report" section — otherwise it would
    # keep naming the last test day's non-reporters indefinitely. Set
    # DAILY_REPORTS_ENABLED=true in Render's mgmg-shared group to start.
    daily_reports_enabled: bool = False

    # --- Task tracker (A3 in the owner's AI agent plan) ---
    # Deadline reminders to employees, one overdue notice to the Director, and
    # the Friday scorecard. On by default: it only ever acts on a deadline the
    # Director set, so nothing is sent until a task has one. Set
    # TASK_TRACKER_ENABLED=false in Render's mgmg-shared group to pause it.
    task_tracker_enabled: bool = True

    # --- Data quality (B4) ---
    # Monday 08:00 report to the admin chat (IT), never to the Director:
    # invoices with no sales person, stale or capped SAP feeds, employees
    # without names, undated tasks, unreadable payment dates.
    data_quality_enabled: bool = True

    # --- 30-day cash calendar (B2) ---
    # Monday 08:00 to the Director and accountants: open invoices by due date
    # coming in, approved written payments going out.
    cash_calendar_enabled: bool = True

    # --- Lead hand-out (agents/lead-handout) ---
    # 08:00 one lead to each B2B sales person, 15:00 "how is it going?" for
    # every open lead. On by default: the owner asked for it (2026-09-30).
    # LEAD_HANDOUT_ENABLED=false pauses both.
    lead_handout_enabled: bool = True

    # --- Team cheer (agents/team-cheer) ---
    # 10:00 encouragement, 14:00 joke/fun question, 17:35 thanks + "how was
    # your day", to every employee but the Director. On by default: the owner
    # asked for it (2026-09-29). TEAM_CHEER_ENABLED=false pauses it.
    team_cheer_enabled: bool = True

    # --- Monthly KPI (E1) ---
    # 08:00 on the 1st to the Director and HR: last month per person.
    monthly_kpi_enabled: bool = True

    # --- Written permission requests (EMJ-SOP-ADM-01) ---
    # The SOP allows an electronic approval only in the system the director
    # officially designates, with the approver and the decision history kept
    # (§3) -- which is why every request here stores who decided, when, and an
    # append-only event trail. Have the director sign the one-page addendum
    # naming this bot before relying on it.
    permissions_enabled: bool = True
    # Deputies who may also decide, besides whoever holds the Director role:
    # comma-separated Telegram user ids. The SOP lets a deputy decide only
    # with written authority (§3), so this list is deliberately explicit
    # rather than derived from a role. Empty = the Director alone decides.
    permission_deputy_telegram_ids: str = ""
    # B1 payment gate (owner's AI agent plan): who decides by amount, as
    # "limit_in_som:telegram_id" pairs, e.g. "5000000:111,20000000:222" =
    # up to 5 mln so'm -> 111, up to 20 mln -> 222, above that -> the Director.
    # The limits are the business's to write, so empty (the default) keeps
    # every request going to the Director and deputies as before. Only whole
    # so'm amounts are routed; "0", other currencies and unreadable amounts
    # always go to the Director.
    permission_approval_tiers: str = ""

    # --- Lead Agent sources ---
    serpapi_api_key: SecretStr = SecretStr("")
    tavily_api_key: SecretStr = SecretStr("")
    # eTender UZEX / xt-xarid / data.egov.uz endpoints: added once confirmed
    # against the real n8n workflow (their sites serve an SPA shell, not JSON,
    # at any plausible guessed API path — see integrations/tenders/README.md).

    # --- AI provider (Lead Agent qualification) ---
    # Both OpenRouter and DeepSeek's own API are OpenAI-compatible chat
    # completion endpoints, so one client handles either — this setting picks
    # which one, so switching back later is a one-line env change, not a
    # code change.

    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_model: str = "google/gemini-3.8-flash"
    # Comma-separated models tried in order when the primary fails. Free
    # (":free") models share a congested pool and returned 429 on 3 of 4 local
    # test runs, so a chain ending on cheap paid capacity is what makes this
    # agent survive unattended every morning.
    openrouter_fallback_models: str = "google/gemini-3.7-flash"


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
    def permission_deputy_ids(self) -> list[int]:
        """Deputy approvers' Telegram ids, parsed from the comma-separated setting.

        Returns:
            Numeric ids; anything unparseable is skipped rather than raising,
            so one typo in the dashboard can't take the whole flow down.
        """
        ids: list[int] = []
        for part in self.permission_deputy_telegram_ids.split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                ids.append(int(part))
        return ids

    @property
    def public_url(self) -> str:
        """The address printed on QR codes ("" when neither setting is known)."""
        return (self.public_base_url or self.render_external_url).strip().rstrip("/")

    @property
    def onec_configured(self) -> bool:
        """1C's OData address, login and password are all set."""
        return bool(
            self.onec_odata_url.strip().startswith("https://")
            and self.onec_login.strip()
            and self.onec_password.get_secret_value()
        )

    @property
    def billz_configured(self) -> bool:
        """BILLZ is switched on and has its integration key."""
        return bool(self.billz_enabled and self.billz_secret_token.get_secret_value().strip())

    @property
    def verifix_auth(self) -> str | None:
        """'oauth' (client id + secret), 'basic' (login + password + filial id), or None."""
        if not self.verifix_enabled:
            return None
        if self.verifix_client_id.strip() and self.verifix_client_secret.get_secret_value():
            return "oauth"
        if self.verifix_login.strip() and self.verifix_password.get_secret_value() and self.verifix_filial_id.strip():
            return "basic"
        return None

    @property
    def verifix_configured(self) -> bool:
        """Verifix is switched on and has a complete way to log in."""
        return self.verifix_auth is not None

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
