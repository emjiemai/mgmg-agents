"""Agent — holiday and birthday greetings, every morning (2026-11-18, the Boss's task T-2026-11-0052).

Reads the «Байрамлар» and «Туғилган кунлар» tabs of the duties register's Google Sheet (filled in advance by the Boss
or HR) and greets each person whose day it is, once; the Director is told of birthdays. See
``integrations/org_bot/greetings.py``.

Run:
    python agents/greetings/agent.py
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

# Agents are run as scripts (see run_morning_agents.py): put the project root on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from integrations.common.db import close_pool  # noqa: E402
from integrations.common.logging_setup import setup_logging  # noqa: E402
from integrations.common.timeutil import today_local  # noqa: E402
from integrations.org_bot import greetings  # noqa: E402

log = setup_logging(greetings.AGENT)


async def run() -> int:
    try:
        counts = await greetings.send_day(today_local(), uuid.uuid4())
        log.info("Greetings: {}", counts)
        return 0
    except Exception as exc:  # noqa: BLE001 — never stops the morning job
        log.error("Greetings failed: {}", type(exc).__name__)
        return 1
    finally:
        await close_pool()


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
