"""Telegram rich messages as plain text (2026-11-17).

Bot API 10.1 (June 2026) added ``Message.rich_message``: a message a person formats with lists, headings or tables
arrives as blocks, not in ``text``. The bot only read ``text``, so on 17.11 the Boss's task written as a numbered list
(«1. Primus 2. Garmin 3. Londry») looked empty and was dropped without a word. ``plain_text`` turns the blocks back
into the text the person sees: paragraphs on their own lines, list items with their label («1.», «•», «☑»), table
rows with cells separated by « | », quotes and captions kept, media left out (a file in a rich message is not read).
"""

from __future__ import annotations

from typing import Any

MAX_DEPTH = 12  # a malformed or hostile message can't recurse forever


def rich_text(value: Any, depth: int = 0) -> str:
    """RichText → str: a string as is, an array joined, an object's ``text`` (or what stands for it)."""
    if depth > MAX_DEPTH or value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(rich_text(v, depth + 1) for v in value)
    if isinstance(value, dict):
        if "text" in value:
            return rich_text(value["text"], depth + 1)
        kind = value.get("type")
        if kind == "custom_emoji":
            return str(value.get("alternative_text") or "")
        if kind == "mathematical_expression":
            return str(value.get("expression") or "")
    return ""


def _item_label(item: dict[str, Any], index: int) -> str:
    if item.get("has_checkbox"):
        return "☑" if item.get("is_checked") else "☐"
    label = str(item.get("label") or "").strip()
    if label:
        return label
    if item.get("value") is not None:
        return f"{item['value']}."
    return "•" if not item.get("type") else f"{index}."


def _blocks(blocks: Any, depth: int, indent: str = "") -> list[str]:
    lines: list[str] = []
    if depth > MAX_DEPTH or not isinstance(blocks, list):
        return lines
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "list":
            for i, item in enumerate(block.get("items") or [], start=1):
                if not isinstance(item, dict):
                    continue
                inner = _blocks(item.get("blocks"), depth + 1, indent + "   ")
                first = inner[0].strip() if inner else ""
                lines.append(f"{indent}{_item_label(item, i)} {first}".rstrip())
                lines.extend(inner[1:])
        elif kind == "table":
            for row in block.get("cells") or []:
                if isinstance(row, list):
                    cells = [rich_text((c or {}).get("text"), depth + 1).strip() for c in row if isinstance(c, dict)]
                    if any(cells):
                        lines.append(indent + " | ".join(cells))
            if block.get("caption"):
                lines.append(indent + rich_text(block["caption"], depth + 1))
        elif kind in ("blockquote", "collage", "slideshow", "details"):
            if kind == "details" and block.get("summary"):
                lines.append(indent + rich_text(block["summary"], depth + 1))
            lines.extend(_blocks(block.get("blocks"), depth + 1, indent))
            for key in ("credit", "caption"):
                if block.get(key):
                    lines.append(indent + rich_text(block[key], depth + 1))
        elif kind == "mathematical_expression":
            lines.append(indent + str(block.get("expression") or ""))
        elif kind in ("divider", "anchor", "map", "buttons", "thinking"):
            continue
        else:  # paragraph, heading, pre, footer, pullquote, expandable_blockquote; media → its caption only
            text = rich_text(block.get("text"), depth + 1) if "text" in block else ""
            if not text and block.get("caption"):
                text = rich_text(block["caption"], depth + 1)
            if block.get("credit"):
                text = f"{text} — {rich_text(block['credit'], depth + 1)}"
            if text.strip():
                lines.append(indent + text)
    return lines


def plain_text(rich_message: Any) -> str:
    """The whole rich message as plain text ("" when nothing readable)."""
    if not isinstance(rich_message, dict):
        return ""
    return "\n".join(_blocks(rich_message.get("blocks"), 0)).strip()


def readable(message: dict[str, Any]) -> dict[str, Any]:
    """The message with ``text`` filled from ``rich_message`` when it has none — every flow reads it as before."""
    if message.get("text") or not message.get("rich_message"):
        return message
    text = plain_text(message["rich_message"])
    return {**message, "text": text} if text else message
