"""FastAPI receiver for the Telegram bots and SAP gateway pushes.

Runs as the ``api`` service in docker-compose (``mgmg-api`` on Render), behind
a reverse proxy.

Endpoints:
    GET  /health                                      liveness + dependency check
    POST /webhooks/telegram/admin/{secret}            Admin Bot (employee access decisions)
    POST /webhooks/telegram/ops/{secret}              OPS Manager Bot (task routing)
    POST /webhooks/sap-push/{secret}                  AR-aging snapshot pushed from the SAP gateway's machine
    POST /webhooks/sap-gateway-push/{tool}/{secret}   every other SAP gateway tool's raw snapshot
    POST /webhooks/sap-data/{dataset}/{secret}        a complete gateway tool's rows (docs/sap-gateway-tools.md)
    POST /webhooks/garmin-lead/{secret}               a lead from the Garmin AI bot (integrations/garmin/leads.py)
    GET  /db, /db/{table}                             read-only database viewer (db_viewer.py)
    GET  /f, /f/{place}, POST /f                      client complaints page behind the QR code (feedback_page.py)

Security:
    * Every webhook path carries a shared secret compared in constant time.
    * The two Telegram routes each check a DIFFERENT secret and each always
      construct ``TelegramBot`` with that specific bot's own token. Telegram
      can only edit a message using the same bot token that sent it, so
      mixing bots on one route would silently break message edits.

Run locally:
    uvicorn integrations.api.app:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from integrations.ai.openrouter_client import describe_openrouter_key
from integrations.api import db_viewer, feedback_page
from integrations.common.config import settings
from integrations.common.db import close_pool, fetch_one
from integrations.common.logging_setup import setup_logging
from integrations.garmin import leads as garmin_leads
from integrations.org_bot import admin as org_admin
from integrations.org_bot import ops_manager as org_ops_manager
from integrations.sap import push_handler as sap_push_handler

AGENT = "api"
log = setup_logging(AGENT)

app = FastAPI(
    title="MGMG Command Center — webhook receiver",
    description="Receives Telegram bot updates and SAP gateway pushes.",
    version="1.0.0",
    docs_url=None,  # no public API docs on an internet-facing service
    redoc_url=None,
)
app.include_router(db_viewer.router)
app.include_router(feedback_page.router)


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Close the PostgreSQL pool when the service stops."""
    await close_pool()


async def _log_ai_config() -> None:
    """Log the models and the OpenRouter key/balance this process actually loaded."""
    try:
        log.info(
            "AI config loaded: OPS Manager Bot model={} fallback={} | OPENROUTER_MODEL={}",
            settings.ops_manager_bot_model,
            settings.ops_manager_bot_fallback_models,
            settings.openrouter_model,
        )
        log.info("{}", await describe_openrouter_key())
    except Exception as exc:  # noqa: BLE001 — a diagnostic must never take the service down
        log.error("AI config check failed: {}", exc)


@app.on_event("startup")
async def _startup() -> None:
    """Run the AI config check in the background so startup isn't delayed."""
    app.state.ai_config_check = asyncio.create_task(_log_ai_config())


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness probe that also verifies the database connection.

    Returns:
        ``{"status": "ok"|"degraded", "database": bool, "writes_enabled": bool}``.
    """
    database_ok = True
    try:
        await fetch_one("SELECT 1 AS ok")
    except Exception as exc:  # noqa: BLE001 — health must never raise
        log.error("Health check: database unreachable: {}", exc)
        database_ok = False

    return {
        "status": "ok" if database_ok else "degraded",
        "database": database_ok,
        "writes_enabled": settings.agent_writes_enabled,
        "dry_run": settings.dry_run,
    }


# ------------------------------------------------------------------ Telegram


@app.post("/webhooks/telegram/admin/{secret}")
async def admin_bot_webhook(secret: str, request: Request) -> dict[str, str]:
    """Receive an Admin Bot update (access Accept/Reject, employee list/remove).

    Register this URL once with:
        https://api.telegram.org/bot<ADMIN_BOT_TOKEN>/setWebhook?url=https://<host>/webhooks/telegram/admin/<secret>

    Args:
        secret: Shared secret from the URL path, checked against
            ``ADMIN_BOT_WEBHOOK_SECRET`` (each route checks its own secret,
            see the module Security note).
        request: The incoming request.

    Returns:
        ``{"status": ...}`` describing what was done.
    """
    if not _secret_ok(secret, settings.admin_bot_webhook_secret.get_secret_value(), "ADMIN_BOT_WEBHOOK_SECRET"):
        return {"status": "unauthorized"}

    if settings.bots_frozen:
        return {"status": "frozen"}

    update = await request.json()
    run_id = uuid.uuid4()

    callback = update.get("callback_query")
    if callback:
        outcome = await org_admin.handle_admin_callback(callback, run_id)
        return {"status": outcome}

    message = update.get("message")
    if message:
        outcome = await org_admin.handle_admin_message(message, run_id)
        return {"status": outcome}

    return {"status": "ignored"}


@app.post("/webhooks/telegram/ops/{secret}")
async def ops_manager_bot_webhook(secret: str, request: Request, background: BackgroundTasks) -> dict[str, str]:
    """Receive an OPS Manager Bot update (a Director task, a role pick, Mark Done).

    Register this URL once with:
        https://api.telegram.org/bot<OPS_MANAGER_BOT_TOKEN>/setWebhook?url=https://<host>/webhooks/telegram/ops/<secret>

    A Director's free-text task is classified and dispatched via
    ``background`` rather than inline (see ``ops_manager.py``'s module
    docstring): a slow AI call must not risk a Telegram-side retry re-running
    classification and double-dispatching the task.

    Args:
        secret: Shared secret from the URL path, checked against
            ``OPS_MANAGER_BOT_WEBHOOK_SECRET``.
        request: The incoming request.
        background: FastAPI background queue.

    Returns:
        ``{"status": ...}`` describing what was done.
    """
    if not _secret_ok(
        secret, settings.ops_manager_bot_webhook_secret.get_secret_value(), "OPS_MANAGER_BOT_WEBHOOK_SECRET"
    ):
        return {"status": "unauthorized"}

    if settings.bots_frozen:
        return {"status": "frozen"}

    update = await request.json()
    outcome = await org_ops_manager.handle_update(update, uuid.uuid4(), background)
    return {"status": outcome}


# ----------------------------------------------------------------------- SAP


@app.post("/webhooks/sap-push/{secret}")
async def sap_push_webhook(secret: str, request: Request) -> dict[str, Any]:
    """Receive an AR-aging snapshot pushed from the SAP gateway's own machine.

    The gateway is deliberately loopback-only and stays that way — this is
    the other half of that design: instead of us reaching in, a plain
    PowerShell script on that machine pushes here on a schedule (see
    ``scripts/sap-gateway-push/``). Unlike the Telegram webhooks, there's no
    external retry behavior to accommodate (nothing re-sends this on a
    non-2xx reply), so this runs the write inline rather than via
    ``BackgroundTasks``.

    Register the pushing script with this URL:
        https://<host>/webhooks/sap-push/<SAP_PUSH_WEBHOOK_SECRET>

    Args:
        secret: Shared secret from the URL path, checked against
            ``SAP_PUSH_WEBHOOK_SECRET`` (not any other route's secret).
        request: The incoming request — body is ``{"invoices": [...]}``.

    Returns:
        ``{"ok": bool, "written": int, "skipped": int}`` on success, or
        ``{"ok": False, "error": "unauthorized"}`` if the secret is wrong.
    """
    if not _secret_ok(secret, settings.sap_push_webhook_secret.get_secret_value(), "SAP_PUSH_WEBHOOK_SECRET"):
        return {"ok": False, "error": "unauthorized"}

    payload = await request.json()
    return await sap_push_handler.handle_ar_aging_push(payload, uuid.uuid4())


@app.post("/webhooks/sap-gateway-push/{tool}/{secret}")
async def sap_gateway_push_webhook(tool: str, secret: str, request: Request) -> dict[str, Any]:
    """Receive a raw snapshot from one of the SAP gateway's other tools.

    Covers everything except get_invoices, which has its own dedicated
    route/table above (``sap_push_webhook``) with real aging-bucket logic.
    These six (orders/products/customers/warehouses/inventory/payments) are
    stored as raw JSON — see ``push_handler.handle_gateway_push`` for why.

    Register the pushing script with URLs shaped like:
        https://<host>/webhooks/sap-gateway-push/inventory/<SAP_PUSH_WEBHOOK_SECRET>
        https://<host>/webhooks/sap-gateway-push/customers/<SAP_PUSH_WEBHOOK_SECRET>
        (etc. — one URL per tool, same secret for all of them)

    Args:
        tool: One of ``push_handler.VALID_TOOLS`` — which gateway tool this
            batch came from.
        secret: Shared secret from the URL path, checked against the same
            ``SAP_PUSH_WEBHOOK_SECRET`` as the AR-aging route.
        request: The incoming request — body is ``{"rows": [...]}``.

    Returns:
        ``{"ok": bool, "written": int}`` on success, or
        ``{"ok": False, "error": "..."}`` on a wrong secret or unknown tool.
    """
    if not _secret_ok(secret, settings.sap_push_webhook_secret.get_secret_value(), "SAP_PUSH_WEBHOOK_SECRET"):
        return {"ok": False, "error": "unauthorized"}

    payload = await request.json()
    return await sap_push_handler.handle_gateway_push(tool, payload, uuid.uuid4())


@app.post("/webhooks/sap-data/{dataset}/{secret}")
async def sap_data_webhook(dataset: str, secret: str, request: Request) -> dict[str, Any]:
    """Receive every row of one kind from a complete gateway tool (2026-10-03).

    The push script (``scripts/sap-gateway-push/``) sends one POST per kind —
    open invoices, sales lines, stock value… (``push_handler.FULL_DATASETS``,
    the tools in ``docs/sap-gateway-tools.md``) — with no row cap; each
    replaces today's rows of that kind. Same secret as the other SAP routes.

    Returns:
        ``{"ok": bool, "written": int, "skipped": int}``, or
        ``{"ok": False, "error": "..."}`` on a wrong secret, unknown kind or bad body.
    """
    if not _secret_ok(secret, settings.sap_push_webhook_secret.get_secret_value(), "SAP_PUSH_WEBHOOK_SECRET"):
        return {"ok": False, "error": "unauthorized"}
    try:
        payload = await request.json()
    except ValueError:
        return {"ok": False, "error": "body is not JSON"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "body must be {\"rows\": [...]}"}
    return await sap_push_handler.handle_full_push(dataset, payload, uuid.uuid4())


@app.post("/webhooks/garmin-lead/{secret}")
async def garmin_lead_webhook(secret: str, request: Request) -> JSONResponse:
    """Store a lead (or a later phone number) sent by the Garmin AI bot.

    Unlike the SAP routes, failures answer with an HTTP error status: the bot
    retries on 5xx and network errors, and gives up on 4xx (a wrong secret or
    a malformed lead won't get better by retrying).
    """
    if not _secret_ok(secret, settings.garmin_leads_secret.get_secret_value(), "GARMIN_LEADS_SECRET"):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "body is not JSON"}, status_code=400)
    event, error = garmin_leads.clean(payload)
    if error:
        log.warning("Garmin lead rejected: {}", error)
        return JSONResponse({"ok": False, "error": error}, status_code=422)
    try:
        result = await garmin_leads.save(event)
    except Exception as exc:  # noqa: BLE001 — a 5xx makes the bot retry later
        log.error("Could not store a Garmin lead: {}", exc)
        return JSONResponse({"ok": False, "error": "storage failed"}, status_code=503)
    return JSONResponse(result)


# -------------------------------------------------------------------- helpers


def _secret_ok(provided: str, expected: str, setting_name: str) -> bool:
    """Compare webhook secrets in constant time.

    Args:
        provided: Secret from the request path.
        expected: Configured secret.
        setting_name: Env var name to name in the log if unconfigured — each
            webhook route checks a different secret.

    Returns:
        True only when both are non-empty and equal. An unset secret always
        fails — an open webhook endpoint is never the safe default.
    """
    if not expected or expected.startswith("["):
        log.error("{} is not configured — rejecting all webhook calls", setting_name)
        return False
    return secrets.compare_digest(provided, expected)
