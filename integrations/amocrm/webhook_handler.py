"""FastAPI receiver for amoCRM webhooks and Telegram callbacks.

Runs as the ``api`` service in docker-compose, behind a reverse proxy.

Endpoints:
    GET  /health                         liveness + dependency check
    POST /webhooks/amocrm/{secret}       amoCRM deal events
    POST /webhooks/telegram/{secret}     Telegram updates (approval buttons)

Security:
    * Both webhook paths carry a shared secret compared in constant time.
      amoCRM does not sign its webhooks, so a secret in the path is the
      available authentication; keep the URL out of shared documents.
    * Payloads are persisted **before** any processing, so a crash mid-handler
      leaves a replayable row rather than a lost event
      (``agents/amocrm-followup/agent.py --drain``).
    * Handlers always answer 200 once the event is stored. amoCRM disables a
      webhook after repeated non-2xx replies, and a stored event loses nothing.

Run locally:
    uvicorn integrations.amocrm.webhook_handler:app --reload --port 8000
"""

from __future__ import annotations

import json
import secrets
import uuid
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request, Response, status

from integrations.common.agent_loader import load_agent
from integrations.common.config import settings
from integrations.common.db import close_pool, execute, fetch_one, log_action
from integrations.common.logging_setup import setup_logging
from integrations.common.money import to_tiyin
from integrations.telegram.bot import TelegramBot

AGENT = "amocrm-webhook"
log = setup_logging(AGENT)

app = FastAPI(
    title="MGMG Command Center — webhook receiver",
    description="Receives amoCRM deal events and Telegram approval callbacks.",
    version="1.0.0",
    docs_url=None,  # no public API docs on an internet-facing service
    redoc_url=None,
)


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Close the PostgreSQL pool when the service stops."""
    await close_pool()


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


# ------------------------------------------------------------------- amoCRM


@app.post("/webhooks/amocrm/{secret}")
async def amocrm_webhook(secret: str, request: Request, background: BackgroundTasks) -> Response:
    """Receive an amoCRM deal event.

    amoCRM posts ``application/x-www-form-urlencoded`` bodies with bracketed
    keys (``leads[status][0][id]``); some accounts post JSON. Both are handled.

    Args:
        secret: Shared secret from the URL path.
        request: The incoming request.
        background: FastAPI background queue used to process after replying.

    Returns:
        200 once the event is stored, 401 on a bad secret.
    """
    if not _secret_ok(secret, settings.amocrm_webhook_secret.get_secret_value()):
        log.warning("amoCRM webhook rejected — bad secret from {}", request.client.host if request.client else "?")
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    payload = await _parse_body(request)
    events = _extract_lead_events(payload)

    if not events:
        log.debug("amoCRM webhook carried no lead events: {}", str(payload)[:300])
        return Response(status_code=status.HTTP_200_OK)

    for event in events:
        event_id = await _store_event(event, payload)
        if event["lead_id"] is not None:
            background.add_task(_process_event, event_id, int(event["lead_id"]))

    log.info("amoCRM webhook: {} event(s) queued", len(events))
    return Response(status_code=status.HTTP_200_OK)


async def _store_event(event: dict[str, Any], raw: dict[str, Any]) -> int:
    """Persist one webhook event before it is processed.

    Args:
        event: Normalized event fields.
        raw: The full original payload, kept for replay.

    Returns:
        The new ``amocrm_deal_events`` row id.

    Raises:
        psycopg.Error: on a database failure.
    """
    row = await fetch_one(
        """
        INSERT INTO amocrm_deal_events
            (event_type, lead_id, pipeline_id, status_id,
             responsible_user_id, price_tiyin, raw_payload)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            event["event_type"],
            event["lead_id"],
            event["pipeline_id"],
            event["status_id"],
            event["responsible_user_id"],
            event["price_tiyin"],
            json.dumps(raw, ensure_ascii=False, default=str),
        ),
    )
    return int(row["id"])  # type: ignore[index]


async def _process_event(event_id: int, lead_id: int) -> None:
    """Run the follow-up agent for one event, recording the outcome.

    Runs in the background after the 200 has already gone back to amoCRM, so a
    slow Graph call can never cause amoCRM to retry or disable the webhook.

    Args:
        event_id: Row id in ``amocrm_deal_events``.
        lead_id: The deal to evaluate.
    """
    try:
        followup = load_agent("amocrm-followup")
        result = await followup.process_lead(lead_id)
        outcome = "processed" if result["action"] == "created" else "ignored"
        await execute(
            "UPDATE amocrm_deal_events SET processing_status = %s, processed_at = now() WHERE id = %s",
            (outcome, event_id),
        )
    except Exception as exc:  # noqa: BLE001 — a background failure must be recorded, not raised
        log.error("Processing event {} (lead {}) failed: {}", event_id, lead_id, exc)
        await execute(
            """
            UPDATE amocrm_deal_events
            SET processing_status = 'failed', processed_at = now(), processing_error = %s
            WHERE id = %s
            """,
            (str(exc)[:1000], event_id),
        )
        await log_action(
            agent=AGENT,
            action="process_event",
            target_system="amocrm",
            status="failure",
            target_ref=f"lead:{lead_id}",
            mode="write",
            error_message=str(exc),
        )


# ------------------------------------------------------------------ Telegram


@app.post("/webhooks/telegram/{secret}")
async def telegram_webhook(secret: str, request: Request) -> dict[str, str]:
    """Receive a Telegram update and resolve approval button presses.

    Register this URL once with:
        https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<host>/webhooks/telegram/<secret>

    Args:
        secret: Shared secret from the URL path.
        request: The incoming request.

    Returns:
        ``{"status": ...}`` describing what was done.
    """
    if not _secret_ok(secret, settings.amocrm_webhook_secret.get_secret_value()):
        return {"status": "unauthorized"}

    update = await request.json()
    callback = update.get("callback_query")
    if not callback:
        return {"status": "ignored"}

    async with TelegramBot(agent=AGENT, run_id=uuid.uuid4()) as bot:
        outcome = await bot.handle_callback_query(callback)

    return {"status": outcome}


# -------------------------------------------------------------------- helpers


def _secret_ok(provided: str, expected: str) -> bool:
    """Compare webhook secrets in constant time.

    Args:
        provided: Secret from the request path.
        expected: Configured secret.

    Returns:
        True only when both are non-empty and equal. An unset secret always
        fails — an open webhook endpoint is never the safe default.
    """
    if not expected or expected.startswith("["):
        log.error("AMOCRM_WEBHOOK_SECRET is not configured — rejecting all webhook calls")
        return False
    return secrets.compare_digest(provided, expected)


async def _parse_body(request: Request) -> dict[str, Any]:
    """Parse a webhook body as JSON or form-encoded data.

    Args:
        request: The incoming request.

    Returns:
        The payload as a dict; ``{}`` if the body is empty or unparseable.
    """
    content_type = request.headers.get("content-type", "")
    try:
        if "application/json" in content_type:
            return await request.json()
        form = await request.form()
        return {key: value for key, value in form.multi_items()}
    except Exception as exc:  # noqa: BLE001 — a malformed body is data, not a crash
        log.warning("Could not parse webhook body: {}", exc)
        return {}


def _extract_lead_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull deal events out of either payload shape.

    Form payloads arrive flattened as ``leads[status][0][id]``; JSON payloads
    arrive nested under ``leads.{add,update,status}``.

    Args:
        payload: The parsed webhook body.

    Returns:
        Normalized events with keys ``event_type``, ``lead_id``,
        ``pipeline_id``, ``status_id``, ``responsible_user_id``, ``price_tiyin``.
    """
    events: list[dict[str, Any]] = []

    # JSON shape
    leads = payload.get("leads")
    if isinstance(leads, dict):
        for event_type, items in leads.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    events.append(_normalize(event_type, item))
        if events:
            return events

    # Form shape: leads[status][0][id] = 123
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for key, value in payload.items():
        if not key.startswith("leads["):
            continue
        parts = [p.strip("]") for p in key.split("[")[1:]]
        if len(parts) < 3:
            continue
        event_type, index, field = parts[0], parts[1], parts[2]
        grouped.setdefault((event_type, index), {})[field] = value

    for (event_type, _index), item in grouped.items():
        events.append(_normalize(event_type, item))

    return events


def _normalize(event_type: str, item: dict[str, Any]) -> dict[str, Any]:
    """Map one raw event item onto our normalized event fields.

    Args:
        event_type: 'add' | 'update' | 'status' | 'delete'.
        item: The raw event fields.

    Returns:
        Normalized event dict with integer ids and price in tiyin.
    """
    return {
        "event_type": event_type,
        "lead_id": _int(item.get("id")),
        "pipeline_id": _int(item.get("pipeline_id")),
        "status_id": _int(item.get("status_id")),
        "responsible_user_id": _int(item.get("responsible_user_id")),
        "price_tiyin": to_tiyin(item.get("price")) if item.get("price") not in (None, "") else None,
    }


def _int(value: Any) -> int | None:
    """Parse an integer from a webhook field, returning None on failure."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None
