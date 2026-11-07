"""Create a verified staff account directly (for the first owner / HR, who nobody can invite yet).

Run on the server, by the IT admin, with the person present:

    python -m scripts.add_staff --tg 123456789 --name "Фамилия Исм Отасининг исми" \
        --role hr --department HR --position "HR масъули"

The person's Telegram ID: they write /start to the bot, and HARI shows it.
Every later employee is added from the bot (➕ Янги ходим) and joins through a one-time link.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from hari.db.models import ROLES
from hari.db.session import create_schema, session_scope
from hari.employees import CardInput, by_telegram, create_card
from hari.worktime import now_utc, today_local


async def run(args: argparse.Namespace) -> None:
    await create_schema()
    async with session_scope() as session:
        if await by_telegram(session, args.tg):
            print(f"Telegram {args.tg} is already linked to an employee. Nothing changed.")
            return
        result = await _create(session, args)
    print(result)  # after the save: a console problem can no longer undo it


async def _create(session, args: argparse.Namespace) -> str:  # noqa: ANN001
    emp, created = await create_card(session, CardInput(
        full_name=args.name, department=args.department, position=args.position, role=args.role,
        start_date=today_local(), language=args.lang,
    ), created_by=None)
    if emp.telegram_user_id and emp.telegram_user_id != args.tg:
        return f"{emp.code} already exists with another Telegram account. Nothing changed."
    emp.telegram_user_id, emp.identity_verified_at, emp.status = args.tg, now_utc(), "active"
    return f"{'Created' if created else 'Linked'} {emp.code} {emp.full_name} ({emp.role})."


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # Cyrillic names on a Windows console
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tg", type=int, required=True, help="Telegram user id")
    parser.add_argument("--name", required=True, help="Full name, as in documents")
    parser.add_argument("--role", choices=ROLES, default="hr")
    parser.add_argument("--department", default="HR")
    parser.add_argument("--position", default="HR масъули")
    parser.add_argument("--lang", choices=("uz_cyrl", "uz_latn", "ru"), default="uz_cyrl")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
