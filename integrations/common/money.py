"""Money handling.

Rule for the whole project: monetary amounts are stored and passed around as
**integer tiyin** (1 UZS = 100 tiyin). Floats are only ever accepted at the
boundary, where an external API hands us one, and are converted immediately.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from integrations.common.config import settings

TIYIN_PER_UZS = 100
NBSP = " "  # keeps "12 345" from wrapping mid-number in Telegram


def to_tiyin(amount: Decimal | float | int | str | None) -> int:
    """Convert a currency amount to integer tiyin.

    Args:
        amount: Amount in whole currency units (e.g. 1250.75 UZS). ``None`` and
            unparseable values become 0.

    Returns:
        The amount in tiyin, rounded half-up (125075 for 1250.75).
    """
    if amount is None:
        return 0
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return 0
    return int((value * TIYIN_PER_UZS).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_tiyin(tiyin: int) -> Decimal:
    """Convert integer tiyin back to whole currency units.

    Args:
        tiyin: Amount in tiyin.

    Returns:
        A ``Decimal`` with two decimal places.
    """
    return (Decimal(tiyin) / TIYIN_PER_UZS).quantize(Decimal("0.01"))


def format_uzs(tiyin: int, *, with_currency: bool = True, decimals: bool = False) -> str:
    """Format tiyin as Uzbek sum with space thousand separators.

    Args:
        tiyin: Amount in tiyin.
        with_currency: Append the "so'm" suffix.
        decimals: Show tiyin as two decimals. Off by default — sum amounts are
            large enough that tiyin are noise in a CEO brief.

    Returns:
        e.g. ``"1 250 000 so'm"`` (with non-breaking spaces as separators).
    """
    value = from_tiyin(tiyin)
    if decimals:
        whole, frac = f"{value:.2f}".split(".")
        body = f"{_group(whole)},{frac}"
    else:
        body = _group(f"{value:.0f}")
    return f"{body}{NBSP}so'm" if with_currency else body


def format_uzs_short(tiyin: int) -> str:
    """Format tiyin compactly for headline figures.

    Uses Uzbek scale abbreviations: mln (10^6), mlrd (10^9).

    Args:
        tiyin: Amount in tiyin.

    Returns:
        e.g. ``"1,25 mlrd so'm"``, ``"340 mln so'm"``, ``"85 000 so'm"``.
    """
    uzs = abs(from_tiyin(tiyin))
    sign = "-" if tiyin < 0 else ""
    if uzs >= 1_000_000_000:
        return f"{sign}{_decimal_comma(uzs / 1_000_000_000)}{NBSP}mlrd{NBSP}so'm"
    if uzs >= 1_000_000:
        return f"{sign}{_decimal_comma(uzs / 1_000_000)}{NBSP}mln{NBSP}so'm"
    return format_uzs(tiyin)


def uzs_to_usd(tiyin: int) -> Decimal:
    """Convert tiyin to approximate USD at the configured reference rate.

    The rate is a static reference from .env, not a live market rate — use it
    for orientation only, never for accounting.

    Args:
        tiyin: Amount in tiyin.

    Returns:
        Approximate USD value, two decimal places.
    """
    rate = Decimal(settings.usd_uzs_reference_rate)
    return (from_tiyin(tiyin) / rate).quantize(Decimal("0.01"))


def _group(digits: str) -> str:
    """Insert non-breaking-space separators every three digits from the right."""
    negative = digits.startswith("-")
    digits = digits.lstrip("-")
    grouped = f"{int(digits):,}".replace(",", NBSP)
    return f"-{grouped}" if negative else grouped


def _decimal_comma(value: Decimal | float) -> str:
    """Render with one decimal place, comma as the decimal mark (local style)."""
    return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")
