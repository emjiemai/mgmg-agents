"""The printable QR card: red background, the black QR code in the centre.

The code sits on a white square: black modules straight on red are hard for
phone scanners (they read brightness, and red is dark), and the QR standard
requires a light margin around the code. Red card, white square, black code
— the look that was asked for, and it scans.

A caption is drawn when a font with Uzbek Cyrillic is available (DejaVu in
the Docker image, Arial on Windows); without one the card is just the code.
"""

from __future__ import annotations

import io
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont
from qrcode.constants import ERROR_CORRECT_Q

RED = (215, 25, 32)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

CARD_W, CARD_H = 1200, 1500
QR_BOX = 820  # the white square
QUIET_MODULES = 4  # the light margin the QR standard requires

TITLE = "ШИКОЯТ ҚОЛДИРИНГ"
SUBTITLE = "Сканерланг · исмсиз юбориш ҳам мумкин"

_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)
# Letters the caption needs that not every font has.
_UZBEK_LETTERS = "ЎҚҒҲўқғҳ"


def qr_matrix(url: str) -> list[list[bool]]:
    """The QR modules for ``url`` (True = black), without a margin."""
    code = qrcode.QRCode(error_correction=ERROR_CORRECT_Q, border=0)
    code.add_data(url)
    code.make(fit=True)
    return code.get_matrix()


def _glyph(font: ImageFont.FreeTypeFont, char: str) -> bytes:
    """How one character renders, as raw pixels."""
    image = Image.new("L", (font.size * 2, font.size * 2), 0)
    ImageDraw.Draw(image).text((0, 0), char, font=font, fill=255)
    return image.tobytes()


def _font(size: int) -> ImageFont.FreeTypeFont | None:
    """The first installed font that really has Uzbek Cyrillic letters."""
    for path in _FONTS:
        if not Path(path).exists():
            continue
        font = ImageFont.truetype(path, size)
        missing = _glyph(font, "\ue000")  # a private-use codepoint renders as the "missing" box
        if all(_glyph(font, ch) != missing for ch in _UZBEK_LETTERS):
            return font
    return None


def _centered(draw: ImageDraw.ImageDraw, text: str, y: int, font: ImageFont.FreeTypeFont) -> None:
    left, _top, right, _bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(((CARD_W - (right - left)) // 2, y), text, fill=WHITE, font=font)


def card_png(url: str) -> bytes:
    """Draw the card for ``url`` (the feedback page) and return it as PNG bytes."""
    matrix = qr_matrix(url)
    modules = len(matrix) + 2 * QUIET_MODULES
    module_px = QR_BOX // modules
    box = module_px * modules

    card = Image.new("RGB", (CARD_W, CARD_H), RED)
    draw = ImageDraw.Draw(card)
    x0 = (CARD_W - box) // 2
    y0 = (CARD_H - box) // 2
    radius = module_px * 2
    draw.rounded_rectangle((x0 - radius, y0 - radius, x0 + box + radius, y0 + box + radius), radius=radius * 2, fill=WHITE)

    offset = QUIET_MODULES * module_px
    for row, cells in enumerate(matrix):
        for col, dark in enumerate(cells):
            if dark:
                left = x0 + offset + col * module_px
                top = y0 + offset + row * module_px
                draw.rectangle((left, top, left + module_px - 1, top + module_px - 1), fill=BLACK)

    title_font, small_font = _font(72), _font(40)
    if title_font and small_font:
        _centered(draw, TITLE, y0 - radius - 150, title_font)
        _centered(draw, SUBTITLE, y0 + box + radius + 60, small_font)

    out = io.BytesIO()
    card.save(out, format="PNG", dpi=(300, 300))
    return out.getvalue()


def card_geometry(url: str) -> tuple[int, int, int, int]:
    """(x0, y0, module_px, modules incl. margin) — where the code sits, for tests."""
    modules = len(qr_matrix(url)) + 2 * QUIET_MODULES
    module_px = QR_BOX // modules
    box = module_px * modules
    return (CARD_W - box) // 2, (CARD_H - box) // 2, module_px, modules
