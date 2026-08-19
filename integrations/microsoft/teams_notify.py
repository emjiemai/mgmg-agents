"""Teams channel notifications via an Incoming Webhook.

Why not Graph: posting a channel message with **application** permissions
(``ChannelMessage.Send``) is a protected Graph API that Microsoft grants only
on request, and our agents run app-only. An Incoming Webhook on the target
channel needs no tenant approval, so that is what the follow-up agent uses to
ping a manager.

Set ``TEAMS_SALES_WEBHOOK_URL`` to the connector URL of the "03 Armin Sales"
channel (Teams → channel → Connectors → Incoming Webhook). When it is unset,
``notify_channel`` logs the message and reports failure rather than raising, so
a missing webhook degrades the follow-up agent instead of breaking it.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from integrations.common.config import settings
from integrations.common.db import log_action
from integrations.common.http import request_with_retry
from integrations.common.logging_setup import setup_logging

log = setup_logging("teams")


async def notify_channel(
    title: str,
    text: str,
    *,
    facts: dict[str, str] | None = None,
    link_url: str | None = None,
    link_label: str = "Open",
    webhook_url: str | None = None,
    agent: str = "-",
    run_id: uuid.UUID | str | None = None,
) -> bool:
    """Post an adaptive-card-style message to a Teams channel.

    Args:
        title: Card title.
        text: Body text (Markdown is rendered by Teams).
        facts: Optional key/value rows shown as a fact list.
        link_url: Optional URL for an action button.
        link_label: Label for that button.
        webhook_url: Override the configured channel webhook.
        agent: Calling agent name, for the audit row.
        run_id: UUID grouping this run's audit rows.

    Returns:
        True if Teams accepted the message, False if it was skipped (no webhook
        configured, writes gated, dry run) or rejected.
    """
    url = webhook_url or settings.teams_sales_webhook_url

    if not url:
        log.warning("TEAMS_SALES_WEBHOOK_URL is not set — skipping Teams notification '{}'", title)
        await log_action(
            agent=agent,
            action="teams_notify",
            target_system="msgraph",
            status="skipped",
            run_id=run_id,
            mode="notify",
            error_message="TEAMS_SALES_WEBHOOK_URL not configured",
            payload={"title": title},
        )
        return False

    if settings.dry_run or not settings.agent_writes_enabled:
        log.info("[write gate closed] would post to Teams: {} — {}", title, text[:120])
        await log_action(
            agent=agent,
            action="teams_notify",
            target_system="msgraph",
            status="dry_run",
            run_id=run_id,
            mode="notify",
            payload={"title": title, "text": text[:500]},
        )
        return False

    card: dict[str, Any] = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "themeColor": "0B5FFF",
        "summary": title,
        "sections": [
            {
                "activityTitle": title,
                "text": text,
                "facts": [{"name": k, "value": v} for k, v in (facts or {}).items()],
                "markdown": True,
            }
        ],
    }
    if link_url:
        card["potentialAction"] = [
            {"@type": "OpenUri", "name": link_label, "targets": [{"os": "default", "uri": link_url}]}
        ]

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await request_with_retry(client, "POST", url, json=card)
    except httpx.HTTPError as err:
        log.error("Teams notification failed: {}", err)
        await log_action(
            agent=agent,
            action="teams_notify",
            target_system="msgraph",
            status="failure",
            run_id=run_id,
            mode="notify",
            error_message=str(err),
            payload={"title": title},
        )
        return False

    ok = response.status_code < 300
    await log_action(
        agent=agent,
        action="teams_notify",
        target_system="msgraph",
        status="success" if ok else "failure",
        run_id=run_id,
        mode="notify",
        http_status=response.status_code,
        payload={"title": title},
    )
    if not ok:
        log.error("Teams webhook returned HTTP {}: {}", response.status_code, response.text[:200])
    return ok
