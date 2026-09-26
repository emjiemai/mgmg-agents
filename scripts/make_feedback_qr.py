"""Make printable feedback QR cards on your own computer.

Same cards Admin Bot sends for "/qr <place>", for printing in bulk:

    python scripts/make_feedback_qr.py --base https://feedback.example.uz garmin ondry-chilonzor

writes qr-garmin.png, qr-ondry-chilonzor.png (1200×1500 px, 300 dpi —
about 10×12.7 cm) into ./qr-cards. Without --base it uses PUBLIC_BASE_URL.

Print a card only once the address is final: a printed code can't be
changed, so point it at a domain the company controls.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integrations.common.config import settings  # noqa: E402
from integrations.org_bot import feedback, qr_card  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Printable feedback QR cards.")
    parser.add_argument("places", nargs="*", default=["umumiy"], help="place labels, e.g. garmin ondry")
    parser.add_argument("--base", default=settings.public_url, help="public address of mgmg-api")
    parser.add_argument("--out", default="qr-cards", help="output folder")
    args = parser.parse_args()

    base = (args.base or "").rstrip("/")
    if not base.startswith("https://"):
        print("Give the public https:// address with --base (or set PUBLIC_BASE_URL).")
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for place in args.places:
        if not feedback.place_ok(place):
            print(f"skipped '{place}': use lowercase Latin letters, digits and '-'")
            continue
        url = f"{base}/f/{place}"
        (out / f"qr-{place}.png").write_bytes(qr_card.card_png(url, place))
        print(f"{out / f'qr-{place}.png'}  ->  {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
