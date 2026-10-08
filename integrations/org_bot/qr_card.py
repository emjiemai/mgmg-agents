"""The printable QR card: red background, the business's logo, the QR code.

The code sits on a white square: black modules straight on red are hard for
phone scanners (they read brightness, and red is dark), and the QR standard
requires a light margin around the code. No text on the card (asked
2026-09-28) — only the business's logo above the code, in white, so the
Londry and Garmin cards can't be mixed up.

Google review cards (``/r``, 2026-10-08) are the same card in green (the
owner: red is the complaints' colour) with five white stars under the code —
still no text, and a printed review card can't be taken for a complaint card.

Logos live in ``logos/`` with their background cut out (transparent PNG, the
brand's own colour). They are small files, so they're enlarged here with
smoothing and redrawn in white; a larger or vector logo would print sharper
— drop it in with the same name.
"""

from __future__ import annotations

import io
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw
from qrcode.constants import ERROR_CORRECT_Q

RED = (215, 25, 32)
GREEN = (30, 142, 62)  # Google review cards: positive, never the complaint red
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

CARD_W = 1200
CARD_H = 1200  # 10×10 cm at 300 dpi — the code alone
CARD_H_LOGO = 1450  # 10×12.3 cm — logo band on top
LOGO_BAND = 390  # red band above the white square that holds the logo
LOGO_MAX_W, LOGO_MAX_H = 560, 150
QR_BOX = 820  # the code with its margin
QUIET_MODULES = 4  # the light margin the QR standard requires

LOGO_DIR = Path(__file__).resolve().parent / "logos"
# URL key (feedback.PLACES) -> logo file.
LOGOS = {"laundry": "londry.png", "garmin": "garmin.png"}


def qr_matrix(url: str) -> list[list[bool]]:
    """The QR modules for ``url`` (True = black), without a margin."""
    code = qrcode.QRCode(error_correction=ERROR_CORRECT_Q, border=0)
    code.add_data(url)
    code.make(fit=True)
    return code.get_matrix()


def _white_logo(place: str) -> Image.Image | None:
    """The place's logo, enlarged to fit the band and redrawn in white (None if missing)."""
    name = LOGOS.get(place)
    if not name or not (LOGO_DIR / name).exists():
        return None
    alpha = Image.open(LOGO_DIR / name).convert("RGBA").getchannel("A")
    scale = min(LOGO_MAX_W / alpha.width, LOGO_MAX_H / alpha.height)
    size = (round(alpha.width * scale), round(alpha.height * scale))
    # Smooth enlargement, then firm the edges back up so the letters stay crisp.
    alpha = alpha.resize(size, Image.Resampling.LANCZOS).point(lambda v: max(0, min(255, (v - 128) * 2 + 128)))
    logo = Image.new("RGBA", size, WHITE + (0,))
    logo.putalpha(alpha)
    return logo


STAR_ROW_H = 190  # extra red band under the code for the review card's stars
STAR_SIZE = 96


def _star(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float) -> None:
    """A filled five-pointed star centred on (cx, cy), outer radius r."""
    import math

    points = []
    for i in range(10):
        angle = -math.pi / 2 + i * math.pi / 5
        radius = r if i % 2 == 0 else r * 0.45
        points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    draw.polygon(points, fill=WHITE)


def card_png(url: str, place: str | None = None, stars: bool = False) -> bytes:
    """Draw the card for ``url`` and return it as PNG bytes.

    Args:
        url: The page the code opens.
        place: ``feedback.PLACES`` key — puts that business's logo on top.
        stars: The Google review card: green, five white stars under the code.
    """
    logo = _white_logo(place) if place else None
    matrix = qr_matrix(url)
    x0, y0, module_px, modules = card_geometry(url, with_logo=logo is not None)
    box = module_px * modules

    height = (CARD_H_LOGO if logo else CARD_H) + (STAR_ROW_H if stars else 0)
    card = Image.new("RGB", (CARD_W, height), GREEN if stars else RED)
    draw = ImageDraw.Draw(card)
    radius = module_px * 2
    draw.rounded_rectangle((x0 - radius, y0 - radius, x0 + box + radius, y0 + box + radius), radius=radius * 2, fill=WHITE)

    offset = QUIET_MODULES * module_px
    for row, cells in enumerate(matrix):
        for col, dark in enumerate(cells):
            if dark:
                left = x0 + offset + col * module_px
                top = y0 + offset + row * module_px
                draw.rectangle((left, top, left + module_px - 1, top + module_px - 1), fill=BLACK)

    if logo is not None:
        # Centre what the eye sees (the solid letters), not faint edge pixels like "®".
        left, top, right, bottom = logo.getchannel("A").point(lambda v: 255 if v > 200 else 0).getbbox()
        band_bottom = y0 - radius
        x = (CARD_W - (right - left)) // 2 - left
        y = (band_bottom - (bottom - top)) // 2 - top
        card.paste(logo, (x, y), logo)

    if stars:
        row_top = y0 + box + radius
        cy = row_top + (height - row_top) / 2
        gap = STAR_SIZE * 1.25
        for i in range(5):
            _star(draw, CARD_W / 2 + (i - 2) * gap, cy, STAR_SIZE / 2)

    out = io.BytesIO()
    card.save(out, format="PNG", dpi=(300, 300))
    return out.getvalue()


def card_geometry(url: str, with_logo: bool = False) -> tuple[int, int, int, int]:
    """(x0, y0, module_px, modules incl. margin) — where the code sits, for tests."""
    modules = len(qr_matrix(url)) + 2 * QUIET_MODULES
    module_px = QR_BOX // modules
    box = module_px * modules
    x0 = (CARD_W - box) // 2
    if with_logo:
        return x0, LOGO_BAND + module_px * 2, module_px, modules
    return x0, (CARD_H - box) // 2, module_px, modules
