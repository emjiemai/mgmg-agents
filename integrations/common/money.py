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


def format_money(minor_units: int, currency: str | None, *, short: bool = False) -> str:
    """Format an amount in whatever currency it's actually denominated in.

    UZS (the default/assumed currency almost everywhere in this project)
    uses the existing so'm formatting. Anything else -- confirmed necessary
    2026-09-07, after SAP AR invoices turned out to include real
    USD-denominated ones that were previously always run through
    ``format_uzs``/``format_uzs_short`` regardless of their actual
    ``currency`` field -- gets its own symbol/code instead of being forced
    through UZS formatting, which shows the right NUMBER with the wrong
    CURRENCY (a $9,764 invoice is not 9,764 so'm; so'm has no cents, so it
    also silently drops precision USD needs).

    Args:
        minor_units: Amount in the currency's smallest unit (tiyin for UZS,
            cents for USD) -- same "integer, x100" convention regardless of
            which currency it actually is; see ``to_tiyin``.
        currency: Currency code as SAP provides it (e.g. "UZS", "USD").
            ``None``/empty is treated as UZS, matching every existing call
            site's prior assumption.
        short: Use the compact mln/mlrd style for large UZS amounts. No
            established short form for other currencies yet, so this is
            ignored for them -- full precision always shown instead.

    Returns:
        e.g. ``"1 250 000 so'm"``, ``"$9,764.00"``, ``"1234.56 EUR"``.
    """
    code = (currency or "UZS").strip().upper()
    if code in ("", "UZS", "SUM", "SO'M"):
        return format_uzs_short(minor_units) if short else format_uzs(minor_units)

    value = Decimal(minor_units) / TIYIN_PER_UZS
    sign = "-" if value < 0 else ""
    body = f"{abs(value):,.2f}"
    return f"{sign}${body}" if code == "USD" else f"{sign}{body} {code}"


def format_money_by_currency(amounts: list[tuple[int, str | None]], *, short: bool = False) -> str:
    """Format a set of amounts that may span more than one currency.

    Groups by currency and sums within each group -- summing raw minor-unit
    values across different currencies (UZS tiyin + USD cents) would produce
    a meaningless number, exactly the bug ``format_money`` above fixes for a
    single amount. The common case (everything actually is one currency,
    which is true almost everywhere in this project) returns exactly what
    ``format_money`` would for the total; a genuinely mixed set is shown as
    each currency's own subtotal, joined, rather than silently blended.

    Args:
        amounts: (minor_units, currency) pairs, e.g. one per invoice.
        short: Passed through to ``format_money`` for any UZS subtotal.

    Returns:
        e.g. ``"1 250 000 so'm"``, or ``"1 250 000 so'm + $9,764.00"`` if
        the input actually mixed currencies. ``"0 so'm"`` if ``amounts`` is
        empty.
    """
    if not amounts:
        return format_money(0, "UZS", short=short)

    totals: dict[str, int] = {}
    order: list[str] = []
    for minor_units, currency in amounts:
        code = (currency or "UZS").strip().upper() or "UZS"
        if code not in totals:
            totals[code] = 0
            order.append(code)
        totals[code] += minor_units

    return " + ".join(format_money(totals[code], code, short=short) for code in order)


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
