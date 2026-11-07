"""Uzbek Latin <-> Cyrillic. Staff type in both; search works on one form (Cyrillic, lower case)."""

from __future__ import annotations

import re

_APOS = "'ʻʼ`’‘"
_LAT_DIGRAPHS = [
    ("o'", "ў"), ("g'", "ғ"), ("sh", "ш"), ("ch", "ч"),
    ("yo", "ё"), ("yu", "ю"), ("ya", "я"), ("ye", "е"),
]
_LAT_SINGLE = {
    "a": "а", "b": "б", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "ҳ", "i": "и", "j": "ж",
    "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "қ", "r": "р", "s": "с",
    "t": "т", "u": "у", "v": "в", "x": "х", "y": "й", "z": "з", "c": "с", "w": "в",
}
_CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ғ": "gʻ", "д": "d", "е": "e", "ё": "yo", "ж": "j",
    "з": "z", "и": "i", "й": "y", "к": "k", "қ": "q", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ў": "oʻ", "ф": "f", "х": "x", "ҳ": "h",
    "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "ʼ", "ь": "", "ы": "i", "э": "e", "ю": "yu",
    "я": "ya",
}
_VOWELS_CYR = set("аеёиоуўэюя")


def _normalize_apostrophes(text: str) -> str:
    return re.sub(f"[{re.escape(_APOS)}]", "'", text)


def latin_to_cyrillic(text: str) -> str:
    """Lower-case Cyrillic form of (mostly) Latin Uzbek text. Cyrillic passes through."""
    s = _normalize_apostrophes(text.lower())
    out: list[str] = []
    i = 0
    while i < len(s):
        two = s[i:i + 2]
        hit = next((cyr for lat, cyr in _LAT_DIGRAPHS if two == lat), None)
        if hit:
            out.append(hit)
            i += 2
            continue
        ch = s[i]
        if ch == "e" and (i == 0 or not s[i - 1].isalpha()):
            out.append("э")  # word-initial e: ertaga -> эртага
        elif ch == "'":
            out.append("ъ")
        else:
            out.append(_LAT_SINGLE.get(ch, ch))
        i += 1
    return "".join(out)


def cyrillic_to_latin(text: str) -> str:
    """Uzbek Cyrillic -> Latin for people who chose Latin. Keeps capitals and everything non-Cyrillic."""
    out: list[str] = []
    for idx, ch in enumerate(text):
        low = ch.lower()
        if low not in _CYR_TO_LAT:
            out.append(ch)
            continue
        lat = _CYR_TO_LAT[low]
        prev = text[idx - 1].lower() if idx else ""
        if low == "е" and (not prev.isalpha() or prev in _VOWELS_CYR):
            lat = "ye"
        if ch.isupper() and lat:
            nxt = text[idx + 1] if idx + 1 < len(text) else ""
            lat = lat.upper() if nxt.isupper() else lat[0].upper() + lat[1:]
        out.append(lat)
    return "".join(out)


def normalize(text: str) -> str:
    """Search form: Cyrillic, lower case, letters/digits/spaces only."""
    cyr = latin_to_cyrillic(text)
    return re.sub(r"[^0-9a-zа-яёўқғҳ ]+", " ", cyr).strip()
