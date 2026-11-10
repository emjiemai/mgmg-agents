"""What the analyst is doing right now, in one message under the question (2026-11-10, the owner).

An answer takes 10–30 seconds — mostly the AI thinking — and a silent chat feels slower than it is. One message
appears under the question at once («⏳ Саволни тушуняпман…»), changes as the analyst really works («⏳ SAP'дан
сотувни қидиряпман…», «⏳ 1C'дан қолдиқларни оляпман…», «⏳ Жавобни ёзяпман…»), and is deleted when the answer
comes. The words name the actual step (from the tool the AI just called), never a made-up one; plain Uzbek Cyrillic.

Never in the way: sent silently, edited at most every ``MIN_GAP`` seconds, and every Telegram failure is ignored —
the answer still goes.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

from integrations.common.config import settings
from integrations.common.logging_setup import setup_logging
from integrations.telegram.bot import TelegramBot

log = setup_logging("ops-manager-bot")

AGENT = "ops-manager-bot"
MIN_GAP = 1.5  # seconds between edits (Telegram limits edits per chat)
MAX_STEPS = 2  # tools named at once («X ва Y»)

START = "⏳ Саволни тушуняпман…"
WRITING = "⏳ Топганларимни жамлаб, жавобни ёзяпман…"

# What each tool does, as the step's words.
TOOL_WORDS: dict[str, str] = {
    "data_sources": "тизимлар уланишини текширяпман",
    "sap_receivables": "SAP'дан мижозлар қарзини қидиряпман",
    "sap_records": "SAP маълумотларини қидиряпман",
    "onec_balances": "1C'дан қолдиқларни оляпман",
    "onec_turnovers": "1C'дан айланмаларни оляпман",
    "billz_sales": "дўконлар савдосини Billz'дан оляпман",
    "supplier_debt_compare": "етказиб берувчилар қарзини 1C ва SAP'да солиштиряпман",
    "garmin_sap": "Garmin бўйича SAP маълумотларини қидиряпман",
    "garmin_data": "Garmin маълумотларини кўряпман",
    "billz_sap_check": "Billz чекларини SAP билан солиштиряпман",
    "old_stock": "омбордаги эски товарларни қидиряпман",
    "garmin_stock_list": "Garmin қолдиғини санаяпман",
    "employee_report": "ҳисоботни топяпман",
    "attendance": "Verifix'дан давоматни текширяпман",
    "company_data": "бот ёзувларини кўряпман",
}
# query_db: what the SQL reads, by its views (first match wins).
VIEW_WORDS: tuple[tuple[str, str], ...] = (
    (r"sap_sales|sales_month", "SAP'дан сотувни қидиряпман"),
    (r"sap_payments_in", "SAP'дан тушган тўловларни қидиряпман"),
    (r"sap_payments_out", "SAP'дан қилинган тўловларни қидиряпман"),
    (r"ar_open|sap_customers", "SAP'дан мижозлар қарзини қидиряпман"),
    (r"sap_ap_open|sap_supplier_balances", "SAP'дан етказиб берувчилар қарзини қидиряпман"),
    (r"sap_stock|sap_products|sap_item_sales", "SAP'дан омбор қолдиғини қидиряпман"),
    (r"sap_po_open|sap_orders_open", "SAP'дан буюртмаларни қидиряпман"),
    (r"sap_service|sap_equipment", "SAP'дан сервис маълумотларини қидиряпман"),
    (r"bot_tasks", "топшириқларни кўряпман"),
    (r"bot_daily_reports", "ҳисоботларни кўряпман"),
)


def tool_words(name: str, args: Any = None) -> str:
    """The step's words for one tool call."""
    if name == "query_db":
        sql = str((args or {}).get("sql") or "") if isinstance(args, dict) else str(args or "")
        for pattern, words in VIEW_WORDS:
            if re.search(pattern, sql, re.I):
                return words
        return "маълумотлар базасидан қидиряпман"
    return TOOL_WORDS.get(name, "маълумот қидиряпман")


def steps_text(calls: list[tuple[str, Any]]) -> str:
    """«⏳ X ва Y…» for the tools of one round (distinct, at most MAX_STEPS; «ва бошқалар» beyond)."""
    words: list[str] = []
    for name, args in calls:
        w = tool_words(name, args)
        if w not in words:
            words.append(w)
    if not words:
        return WRITING
    text = " ва ".join(words[:MAX_STEPS]) + (" ва бошқаларни" if len(words) > MAX_STEPS else "")
    return f"⏳ {text[0].upper()}{text[1:]}…"


class Progress:
    """One status message for one question: start → update … → done."""

    def __init__(self, chat_id: int, run_id: uuid.UUID, reply_to: int | None = None) -> None:
        self.chat_id, self.run_id, self.reply_to = chat_id, run_id, reply_to
        self.message_id: int | None = None
        self.text = ""
        self.at = 0.0

    def _bot(self) -> TelegramBot:
        return TelegramBot(agent=AGENT, run_id=self.run_id,
                           bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value())

    async def start(self, text: str = START) -> None:
        try:
            async with self._bot() as bot:
                ids = await bot.send_message(text, chat_id=str(self.chat_id), disable_notification=True,
                                             reply_to_message_id=self.reply_to)
            self.message_id, self.text, self.at = (ids[0] if ids else None), text, time.monotonic()
        except Exception as exc:  # noqa: BLE001 — a status line is never worth failing the answer
            log.info("Status message not sent: {}", type(exc).__name__)

    async def update(self, text: str) -> None:
        if not self.message_id or text == self.text or time.monotonic() - self.at < MIN_GAP:
            return
        try:
            async with self._bot() as bot:
                await bot._edit_message(str(self.chat_id), self.message_id, text)  # noqa: SLF001
            self.text, self.at = text, time.monotonic()
        except Exception as exc:  # noqa: BLE001
            log.info("Status message not edited: {}", type(exc).__name__)

    async def done(self) -> None:
        """Delete the status line (before the answer is sent, so the answer is the last message)."""
        message_id, self.message_id = self.message_id, None
        if not message_id:
            return
        try:
            async with self._bot() as bot:
                await bot._call("deleteMessage", {"chat_id": str(self.chat_id), "message_id": message_id},  # noqa: SLF001
                                mode="notify", target_ref=str(self.chat_id))
        except Exception as exc:  # noqa: BLE001
            log.info("Status message not deleted: {}", type(exc).__name__)
