"""Pure-Python string cleanup for daily post inputs.

Three concerns, three transforms:

  1. pack_name_for_canva  — RENDER use (top-right pill text).
     Strips "Pokemon" / "Pokémon" as whole words (case-insensitive),
     collapses whitespace, uppercases the remainder.

  2. pack_name_for_caption — CAPTION use (Instagram post body).
     Same Pokemon-stripping, but title-cased and with the original
     punctuation/spacing preserved so it reads naturally.

  3. card_name_cleanup — RENDER + CAPTION use.
     Database card names from DripShopLive look like:
       "PSA GEM MT 10 FA/GALARIAN MOLTRES V, 2021, POKEMON SWORD & SHIELD CHILLING REIGN, #177"
     Cleans down to a short, post-friendly form:
       "PSA 10 Galarian Moltres V"

  4. format_price — Whole-dollar formatter; "$650" from 650, 649.99, or "650.00".

All transforms are stateless and safe to call multiple times. Empty or
None input returns empty string rather than raising — the renderer's
auto-sizer will choose a fallback font on empty input.
"""

from __future__ import annotations

import re
from typing import Optional, Union

# --------------------------------------------------------------------------- #
# Pack name transforms
# --------------------------------------------------------------------------- #

# Whole-word "Pokemon" / "Pokémon" matcher. Case-insensitive. Strips the
# word and any trailing whitespace that becomes redundant. We don't strip
# "POKEMONFOO" or "MARIOPOKEMON" — both contain the substring but aren't
# the word.
_POKEMON_WORD_RE = re.compile(r"\bpok[eé]mon\b", re.IGNORECASE)

# Common noise patterns we collapse. Multiple spaces, plus leading/trailing
# orphan punctuation that's likely junk after Pokemon-stripping ("," "-" ":"
# can become stranded when the surrounding word goes away). Trailing "!" is
# preserved — it's meaningful in real pack names like "Mystic Mega Pack!".
_WHITESPACE_RE = re.compile(r"\s+")
_LEADING_TRAILING_PUNCT_RE = re.compile(r"^[\s,\-:]+|[\s,\-:]+$")


def pack_name_for_canva(s: Optional[str]) -> str:
    """RENDER form: Pokemon-stripped, all caps, whitespace collapsed.

    Examples:
      "Gold PSA 10 Pokemon Slab Pack"   → "GOLD PSA 10 SLAB PACK"
      "Mystic Mystery Mega Pack!"       → "MYSTIC MYSTERY MEGA PACK!"
      "Don't Like It Raw"               → "DON'T LIKE IT RAW"
    """
    if not s:
        return ""
    cleaned = _POKEMON_WORD_RE.sub("", s)
    cleaned = _LEADING_TRAILING_PUNCT_RE.sub("", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned.upper()


def pack_name_for_caption(s: Optional[str]) -> str:
    """CAPTION form: Pokemon-stripped, title-cased, whitespace collapsed.

    Examples:
      "Gold PSA 10 Pokemon Slab Pack"   → "Gold PSA 10 Slab Pack"
      "Mystic Mystery Mega Pack!"       → "Mystic Mystery Mega Pack!"
      "DON'T LIKE IT RAW"               → "Don't Like It Raw"

    str.title() over-capitalizes contractions ("Don'T") — we post-process
    to re-lowercase the letter after an apostrophe.
    """
    if not s:
        return ""
    cleaned = _POKEMON_WORD_RE.sub("", s)
    cleaned = _LEADING_TRAILING_PUNCT_RE.sub("", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    titled = cleaned.title()
    # Fix str.title()'s over-capitalization of post-apostrophe letters.
    titled = re.sub(r"(\w)'(\w)", lambda m: m.group(0)[0] + "'" + m.group(2).lower(), titled)
    return titled


# --------------------------------------------------------------------------- #
# Card name cleanup
# --------------------------------------------------------------------------- #

# DripShopLive card names follow a structured pattern:
#   "<GRADER> <GRADE_LABEL> <GRADE_NUMBER> <PREFIX/>CARD NAME, YEAR, SET, #NUM"
# Examples (observed in production):
#   "PSA GEM MT 10 FA/GALARIAN MOLTRES V, 2021, POKEMON SWORD & SHIELD CHILLING REIGN, #177"
#   "PSA NM-MT 8 FA/CHARIZARD V, 2022, POKEMON SWSH BLACK STAR PROMO, #260"
#   "CGC 9 Single Strike Urshifu, 2021, Chilling Reign, #108/198"
#   "PSA MINT 9 PIKACHU, 2026, POKEMON TEF EN-TEMPORAL FORCES, #51"
#   "PSA GEM MT 10 SKELEDIRGE ex, 2023, POKEMON PAL EN-PALDEA EVOLVED, #258"
#
# Goal: pull out "<GRADER> <GRADE_NUMBER> <Card Name>" — the part that
# reads cleanly in an Instagram caption. Drop the grade word ("GEM MT",
# "MINT", "NM-MT" etc.), the year, the set, and the card number.
#
# We do this with heuristics rather than a strict parser because the
# database has dozens of grader-text variants. Regex captures the bones;
# title-casing fixes the all-caps loud look.

_GRADER_NAMES = {"PSA", "CGC", "BGS", "BCCG", "TAG"}

# Grade-label words that appear AFTER the grader and BEFORE the numeric
# grade. We strip these to get "PSA 10" from "PSA GEM MT 10". The set
# is intentionally broad — false positives here are harmless because
# they'd be uncommon real words appearing in the same position.
_GRADE_LABELS = {
    "GEM", "MT", "MINT", "NM-MT", "NM", "EX", "EX-MT",
    "VG-EX", "VG", "GD", "GOOD", "PR", "POOR", "FR", "FAIR",
    "PRISTINE", "PERFECT", "MIN",
}

# Prefix tokens (joined to the card name by "/") meaning "full art" etc.
# These are mostly visual modifiers we drop for caption use.
_DROPPABLE_PREFIXES = {"FA", "SA", "AA", "FULL ART", "SECRET ART", "AR"}


def card_name_cleanup(s: Optional[str]) -> str:
    """Compress a verbose grading-service card name into a post-friendly form.

    Examples:
      "PSA GEM MT 10 FA/GALARIAN MOLTRES V, 2021, POKEMON SWORD & SHIELD CHILLING REIGN, #177"
        → "PSA 10 Galarian Moltres V"
      "CGC 9 Single Strike Urshifu, 2021, Chilling Reign, #108/198"
        → "CGC 9 Single Strike Urshifu"
      "PSA MINT 9 PIKACHU, 2026, POKEMON TEF EN-TEMPORAL FORCES, #51"
        → "PSA 9 Pikachu"
      "PSA GEM MT 10 SKELEDIRGE ex, 2023, POKEMON PAL EN-PALDEA EVOLVED, #258"
        → "PSA 10 Skeledirge ex"

    Empty input returns empty string.
    """
    if not s:
        return ""
    # First comma splits the "grader + grade + name" header from year/set/num.
    header = s.split(",", 1)[0].strip()
    tokens = header.split()
    if not tokens:
        return ""

    # 1) Grader: first token if recognized; otherwise leave as-is (the input
    #    might already be a clean card name).
    grader: Optional[str] = None
    if tokens[0].upper() in _GRADER_NAMES:
        grader = tokens[0].upper()
        tokens = tokens[1:]

    # 2) Strip grade-label words between grader and the numeric grade.
    while tokens and tokens[0].upper() in _GRADE_LABELS:
        tokens = tokens[1:]

    # 3) Numeric grade: first token that looks like a number (10, 9, 9.5).
    grade: Optional[str] = None
    if tokens and re.fullmatch(r"\d+(\.\d+)?", tokens[0]):
        grade = tokens[0]
        tokens = tokens[1:]

    # 4) Drop "FA/" / "SA/" / etc. prefixes joined to the card name.
    if tokens:
        first = tokens[0]
        if "/" in first:
            prefix, rest = first.split("/", 1)
            if prefix.upper() in _DROPPABLE_PREFIXES:
                tokens[0] = rest

    # 5) The rest IS the card name. Title-case it (preserve trailing
    #    suffix-letters like "ex", "V", "VMAX" which Pokemon uses lowercase
    #    or specific caps for).
    card = " ".join(tokens).strip()
    card = _title_case_card_name(card)

    parts = [p for p in (grader, grade, card) if p]
    return " ".join(parts)


# Pokemon card suffix words that have specific casing conventions on the
# physical cards. We respect these instead of letting str.title() force them.
_CARD_SUFFIX_CASING = {
    "ex": "ex", "EX": "EX", "v": "V", "V": "V", "vmax": "VMAX", "VMAX": "VMAX",
    "vstar": "VSTAR", "VSTAR": "VSTAR", "gx": "GX", "GX": "GX",
    "tag-team": "Tag-Team", "tag": "Tag", "lv.x": "LV.X",
}


def _title_case_card_name(s: str) -> str:
    """Title-case a card name, preserving Pokemon-specific suffix casing."""
    if not s:
        return s
    words = s.split()
    result: list[str] = []
    for w in words:
        if w.lower() in _CARD_SUFFIX_CASING:
            result.append(_CARD_SUFFIX_CASING[w.lower()])
        else:
            # str.title() handles most words; we preserve apostrophes manually.
            titled = w.title()
            titled = re.sub(r"(\w)'(\w)", lambda m: m.group(0)[0] + "'" + m.group(2).lower(), titled)
            result.append(titled)
    return " ".join(result)


# --------------------------------------------------------------------------- #
# Price formatting
# --------------------------------------------------------------------------- #


def format_price(v: Union[int, float, str, None]) -> str:
    """Whole-dollar price string for use in captions / renders.

    Examples:
      650           → "$650"
      649.99        → "$650"
      "1000.00"     → "$1000"
      None          → "$0"  (defensive — callers should validate non-null first)
    """
    if v is None:
        return "$0"
    try:
        amount = round(float(v))
    except (TypeError, ValueError):
        return "$0"
    return f"${int(amount)}"
