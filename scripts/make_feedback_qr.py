"""Make the printable complaints QR card on your own computer.

The same cards Admin Bot sends for "/qr" — one per business:

    python scripts/make_feedback_qr.py --base https://shikoyat.example.uz

writes qr-shikoyat-laundry.png and qr-shikoyat-garmin.png (1200×1500 px,
300 dpi — about 10×12.7 cm). Without --base it uses PUBLIC_BASE_URL.

Print it only once the address is final: a printed code can't be changed,
so point it at a domain the company controls.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integrations.api.feedback_page import PAGES  # noqa: E402
from integrations.common.config import settings  # noqa: E402
from integrations.org_bot import qr_card  # noqa: E402
from integrations.org_bot.feedback import PLACES  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Printable complaints QR card.")
    parser.add_argument("--base", default=settings.public_url, help="public address of mgmg-api")
    parser.add_argument("--out", default=".", help="output folder")
    args = parser.parse_args()

    base = (args.base or "").rstrip("/")
    if not base.startswith("https://"):
        print("Give the public https:// address with --base (or set PUBLIC_BASE_URL).")
        return 2
    for place, business in PLACES.items():
        url = f"{base}{PAGES[place]}"
        out = Path(args.out) / f"qr-shikoyat-{place}.png"
        out.write_bytes(qr_card.card_png(url, business))
        print(f"{out}  ->  {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
