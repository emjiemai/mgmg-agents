"""Uzbek Latin -> Cyrillic for people's names that arrive from other systems.

Every bot message is Uzbek Cyrillic (the business's rule since 2026-09-25),
but systems such as Verifix keep names the way HR typed them — often Latin
("Salimov Mumin", "G'ofurov Sherzod"). This is the deterministic, offline
conversion of those names; free text is still converted by the AI
(``integrations/org_bot/answer_check.py``).

Rules are the official Uzbek alphabet correspondence plus the Russian-style
spellings found in passports ("kh", "zh"). Names already in Cyrillic are
returned unchanged.
"""

from __future__ import annotations

import re

_APOSTROPHES = "'ʻʼ‘’`"

# Longest first; checked in order at each position.
_MULTI: list[tuple[str, str]] = [
    ("o'", "ў"),
    ("g'", "ғ"),
    ("sh", "ш"),
    ("ch", "ч"),
    ("kh", "х"),
    ("zh", "ж"),
    ("yo", "ё"),
    ("yu", "ю"),
    ("ya", "я"),
    ("ye", "е"),
]
_SINGLE = {
    "a": "а", "b": "б", "c": "с", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "ҳ",
    "i": "и", "j": "ж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п",
    "q": "қ", "r": "р", "s": "с", "t": "т", "u": "у", "v": "в", "w": "в", "x": "х",
    "y": "й", "z": "з",
}
_LATIN = re.compile(r"[A-Za-z]")
_CYRILLIC = re.compile(r"[А-Яа-яЁёЎўҚқҒғҲҳ]")


def _cased(source: str, target: str) -> str:
    return target.upper() if source[:1].isupper() else target


def latin_to_cyrillic(text: str) -> str:
    """Convert Uzbek Latin text to Cyrillic, keeping case, spaces and punctuation."""
    text = re.sub(f"[{_APOSTROPHES}]", "'", text or "")
    out: list[str] = []
    i = 0
    while i < len(text):
        at_word_start = i == 0 or not text[i - 1].isalpha()
        low = text[i : i + 2].lower()
        # "yo'l" is y + o', not yo + an apostrophe.
        pairs = [(a, b) for a, b in _MULTI if not (a == "yo" and text[i + 2 : i + 3] == "'")]
        match = next(((a, b) for a, b in pairs if low == a), None)
        if match is None and at_word_start and low == "ts":
            match = ("ts", "ц")  # Tsoy -> Цой; inside a word "ts" is т+с
        if match is not None:
            out.append(_cased(text[i], match[1]))
            i += 2
            continue
        char = text[i]
        if char == "'":
            out.append("ъ")  # tutuq belgisi (Ma'rufjon -> Маъруфжон)
        elif char.lower() == "e" and at_word_start:
            out.append(_cased(char, "э"))
        elif char.lower() in _SINGLE:
            out.append(_cased(char, _SINGLE[char.lower()]))
        else:
            out.append(char)
        i += 1
    return "".join(out)


def name_to_cyrillic(name: str) -> str:
    """A person's name in Cyrillic: converted if written in Latin, else as it is."""
    name = " ".join((name or "").split())
    if _LATIN.search(name) and not _CYRILLIC.search(name):
        return latin_to_cyrillic(name)
    return name


# One Latin skeleton for names in either script (2026-11-09): the analyst's SQL matches «Хилтон», «Hilton» and
# «Xilton» with it, and folds what a Russian keyboard swaps (қ = к = q, ў = у = o', дж = ж, word-initial ye = е).
# The same steps as database/schema.sql analyst.name_key — selfcheck keeps the two identical.
SKELETON_PAIRS: tuple[tuple[str, str], ...] = (
    ("kh", "h"), ("dj", "j"), ("o'", "u"), ("oʻ", "u"), ("o‘", "u"), ("o’", "u"), ("дж", "j"),
    ("ш", "sh"), ("щ", "sh"), ("ч", "ch"), ("ё", "yo"), ("ю", "yu"), ("я", "ya"), ("ц", "ts"),
)
SKELETON_FROM = "абвгдежзийклмнопрстуфхҳыэўғқxqъь"
SKELETON_TO = "abvgdejziyklmnoprstufhhieugkhk"
_SKELETON_TABLE = {ord(c): (SKELETON_TO[i] if i < len(SKELETON_TO) else None) for i, c in enumerate(SKELETON_FROM)}


def skeleton(text: str | None) -> str:
    """«Хилтон Ташкент», «Xilton  Tashkent!» → «hilton tashkent»: lower, Cyrillic → Latin, x/kh → h, қ/q → k,
    ў/o' → u, дж → j, word-initial ye → e, only a-z 0-9."""
    value = (text or "").lower()
    for a, b in SKELETON_PAIRS:
        value = value.replace(a, b)
    value = re.sub(r"[^a-z0-9 ]", "", value.translate(_SKELETON_TABLE))
    value = re.sub(r"(^| )ye", r"\1e", value)
    return re.sub(r"\s+", " ", value).strip()
