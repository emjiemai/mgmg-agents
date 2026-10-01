"""Leads from the Garmin AI bot, kept in the Command Center's database.

The Garmin bot (separate repo emjiemai/Garmin-AI-bot, @garminofficialuzbot:
Instagram Reels → web catalog → AI Telegram bot → manager) used to keep its
leads only in memory and in a file on Render's free disk, which every
restart wiped. Since 2026-10-01 it also POSTs each lead here:

    POST /webhooks/garmin-lead/{GARMIN_LEADS_SECRET}
    {"event": "lead",  "lead_id": "<uuid>", "chat_id": 123, "urgency": "now"|"next"|"outage",
     "name": ..., "username": ..., "phone": ..., "product_id": ..., "product_name": ...,
     "price": 1234, "budget": ..., "summary": ..., "lang": "ru"|"uz", "source": ..., "at": "<ISO time>"}
    {"event": "phone", "chat_id": 123, "phone": "+998..."}   # a number shared after the lead

``lead_id`` makes a retried POST harmless (stored once). The Director can
ask OPS Manager Bot about them ("garmin lidlari qanday?").
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from integrations.common.db import fetch_all, fetch_one
from integrations.common.timeutil import to_local

URGENCIES = {"now": "🔥 иссиқ", "next": "🟡 илиқ", "outage": "⚠️ бот хатоси"}
_TEXT_LIMITS = {"name": 120, "username": 64, "phone": 32, "product_id": 80, "product_name": 200,
                "budget": 120, "summary": 2000, "lang": 8, "source": 60}


def clean(payload: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate one push; ``(event, None)`` or ``(None, reason)``."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    event = payload.get("event", "lead")
    try:
        chat_id = int(payload.get("chat_id"))
    except (TypeError, ValueError):
        return None, "chat_id must be a number"

    if event == "phone":
        phone = str(payload.get("phone") or "").strip()[:32]
        if not phone:
            return None, "phone is empty"
        return {"event": "phone", "chat_id": chat_id, "phone": phone}, None
    if event != "lead":
        return None, f"unknown event {event!r}"

    lead_id = str(payload.get("lead_id") or "").strip()
    if not 8 <= len(lead_id) <= 64:
        return None, "lead_id is required"
    urgency = payload.get("urgency")
    if urgency not in URGENCIES:
        return None, f"unknown urgency {urgency!r}"
    lead: dict[str, Any] = {"event": "lead", "lead_id": lead_id, "chat_id": chat_id, "urgency": urgency}
    for key, limit in _TEXT_LIMITS.items():
        value = payload.get(key)
        lead[key] = str(value).strip()[:limit] if value not in (None, "") else None
    try:
        lead["price"] = float(payload["price"]) if payload.get("price") not in (None, "") else None
    except (TypeError, ValueError):
        lead["price"] = None
    try:
        lead["at"] = datetime.fromisoformat(str(payload.get("at")).replace("Z", "+00:00"))
    except ValueError:
        lead["at"] = None
    return lead, None


async def save(event: dict[str, Any]) -> dict[str, Any]:
    """Store a lead (once per lead_id) or put a later phone on the customer's latest lead."""
    if event["event"] == "phone":
        row = await fetch_one(
            """UPDATE garmin_leads SET phone = %s, phone_added_at = now()
               WHERE id = (SELECT id FROM garmin_leads WHERE chat_id = %s AND urgency <> 'outage'
                           AND phone IS NULL ORDER BY created_at DESC LIMIT 1)
               RETURNING id""",
            (event["phone"], event["chat_id"]),
        )
        return {"ok": True, "updated": row is not None}
    row = await fetch_one(
        """INSERT INTO garmin_leads (lead_id, chat_id, urgency, name, username, phone, product_id, product_name,
                                     price, budget, summary, lang, source, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()))
           ON CONFLICT (lead_id) DO NOTHING RETURNING id""",
        (event["lead_id"], event["chat_id"], event["urgency"], event["name"], event["username"], event["phone"],
         event["product_id"], event["product_name"], event["price"], event["budget"], event["summary"],
         event["lang"], event["source"], event["at"]),
    )
    return {"ok": True, "stored": row is not None}


async def recent(days: int = 30) -> list[dict[str, Any]]:
    return await fetch_all(
        "SELECT * FROM garmin_leads WHERE created_at > now() - make_interval(days => %s) ORDER BY created_at DESC",
        (days,),
    )


def describe(rows: list[dict[str, Any]], days: int = 30) -> str:
    """Plain-text data for OPS Manager Bot's answers."""
    sales = [r for r in rows if r["urgency"] != "outage"]
    if not rows:
        return (f"No leads from the Garmin AI bot (@garminofficialuzbot) in the last {days} days. Leads are "
                "stored here since 2026-10-01; older ones were only sent to the manager's Telegram chat.")
    hot = sum(1 for r in sales if r["urgency"] == "now")
    with_phone = sum(1 for r in sales if r.get("phone"))
    products: dict[str, int] = {}
    for r in sales:
        if r.get("product_name"):
            products[r["product_name"]] = products.get(r["product_name"], 0) + 1
    top = ", ".join(f"{name} ({n})" for name, n in sorted(products.items(), key=lambda kv: -kv[1])[:5]) or "none named"
    lines = [
        f"Garmin AI bot leads (Instagram → web catalog → Telegram bot → manager), last {days} days: "
        f"{len(sales)} sales leads ({hot} hot = wants to buy now, {len(sales) - hot} warm), {with_phone} with a phone; "
        f"{len(rows) - len(sales)} bot-outage tickets. Most asked-about products: {top}.",
        "Each lead, newest first:",
    ]
    for r in rows[:80]:
        when = to_local(r["created_at"]).strftime("%Y-%m-%d %H:%M")
        who = r.get("name") or (f"@{r['username']}" if r.get("username") else "no name")
        lines.append(
            f"- [{when}] {r['urgency']} | {who} | phone: {r.get('phone') or 'none'} | product: "
            f"{r.get('product_name') or '-'} | source: {r.get('source') or '-'} | {r.get('summary') or ''}"
        )
    return "\n".join(lines)
