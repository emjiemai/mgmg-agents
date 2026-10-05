"""Designed reports: an HTML template filled with the day's numbers, as a PDF or a PNG.

The owner, 2026-10-05: the morning brief as a long Telegram text was
overwhelming for the Director, and reports read better as a page whose
layout never changes — only the numbers do. So each report is a template
in ``templates/`` (Jinja2, autoescaped), laid out by WeasyPrint as a PDF;
when a picture is wanted (the brief, sent as a Telegram photo), pypdfium2
draws that page as a PNG, cut to the content's height.

No browser and no JavaScript: what the template says is what the page shows,
and the fonts are bundled (``fonts/``), so Render and a laptop produce the
same pixels. WeasyPrint needs Pango from the OS (see the Dockerfile).
"""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
FONTS = HERE / "fonts"

# The image page is 540 CSS px wide, drawn at 1080 px: Telegram's own photo
# width, so the picture reaches the phone without being resampled.
IMAGE_WIDTH_CSS = 540
IMAGE_WIDTH_PX = 1080
# Laid out on a tall page, then cut where the content ends (see ``image``).
IMAGE_PAGE_HEIGHT_CSS = 6000
# The canvas below the content is painted this colour, and found again in the pixels.
SENTINEL = (255, 0, 255)


class RenderError(RuntimeError):
    """A template could not be turned into a PDF or an image."""


@lru_cache(maxsize=1)
def _env() -> Any:
    from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

    return Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,  # a missing value fails the render instead of printing nothing
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html(template: str, **context: Any) -> str:
    """Fill one template; ``fonts`` (the bundled fonts' folder URL) is always available."""
    return _env().get_template(template).render(fonts=FONTS.as_uri(), **context)


def html_to_pdf(html: str) -> bytes:
    from weasyprint import HTML

    try:
        return HTML(string=html, base_url=str(TEMPLATES)).write_pdf()
    except Exception as exc:  # noqa: BLE001 — one clear error for the caller's fallback
        raise RenderError(f"PDF layout failed: {exc}") from exc


def pdf(template: str, **context: Any) -> bytes:
    """A template as a PDF document (A4 pages, as the template's @page says)."""
    return html_to_pdf(render_html(template, image=False, **context))


def image(template: str, **context: Any) -> bytes:
    """A one-page template as a PNG, 1080 px wide, exactly as tall as its content."""
    import pypdfium2 as pdfium
    from PIL import Image, ImageChops

    html = render_html(
        template, image=True, page_width=IMAGE_WIDTH_CSS, page_height=IMAGE_PAGE_HEIGHT_CSS,
        sentinel="#{:02x}{:02x}{:02x}".format(*SENTINEL), **context,
    )
    document = pdfium.PdfDocument(html_to_pdf(html))
    if len(document) != 1:
        raise RenderError(f"{template}: the content needs {len(document)} pages, an image holds one")
    page = document[0]
    scale = IMAGE_WIDTH_PX / page.get_width()  # PDF points to pixels
    picture = page.render(scale=scale).to_pil().convert("RGB")
    # Everything that isn't the sentinel colour is content; cut just below it.
    box = ImageChops.difference(picture, Image.new("RGB", picture.size, SENTINEL)).getbbox()
    if box is None:
        raise RenderError(f"{template}: the page is empty")
    picture = picture.crop((0, 0, picture.width, box[3]))
    out = io.BytesIO()
    picture.save(out, format="PNG", optimize=True)
    return out.getvalue()
