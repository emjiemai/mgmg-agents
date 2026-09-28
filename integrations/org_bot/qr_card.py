"""The printable QR card: red background, the black QR code on white, no text.

The code sits on a white square: black modules straight on red are hard for
phone scanners (they read brightness, and red is dark), and the QR standard
requires a light margin around the code. Red card, white square, black code
— nothing written on it (asked 2026-09-28).
"""

from __future__ import annotations

import io

import qrcode
from PIL import Image, ImageDraw
from qrcode.constants import ERROR_CORRECT_Q

RED = (215, 25, 32)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

CARD_W, CARD_H = 1200, 1200  # 10×10 cm at 300 dpi
QR_BOX = 820  # the white square
QUIET_MODULES = 4  # the light margin the QR standard requires


def qr_matrix(url: str) -> list[list[bool]]:
    """The QR modules for ``url`` (True = black), without a margin."""
    code = qrcode.QRCode(error_correction=ERROR_CORRECT_Q, border=0)
    code.add_data(url)
    code.make(fit=True)
    return code.get_matrix()


def card_png(url: str) -> bytes:
    """Draw the card for ``url`` (the feedback page) and return it as PNG bytes."""
    matrix = qr_matrix(url)
    x0, y0, module_px, modules = card_geometry(url)
    box = module_px * modules

    card = Image.new("RGB", (CARD_W, CARD_H), RED)
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

    out = io.BytesIO()
    card.save(out, format="PNG", dpi=(300, 300))
    return out.getvalue()


def card_geometry(url: str) -> tuple[int, int, int, int]:
    """(x0, y0, module_px, modules incl. margin) — where the code sits, for tests."""
    modules = len(qr_matrix(url)) + 2 * QUIET_MODULES
    module_px = QR_BOX // modules
    box = module_px * modules
    return (CARD_W - box) // 2, (CARD_H - box) // 2, module_px, modules
