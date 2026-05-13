"""Tests for src/string_transforms.py — pack/card name cleanup + price."""

from __future__ import annotations

import pytest

from src.string_transforms import (
    card_name_cleanup,
    format_price,
    pack_name_for_canva,
    pack_name_for_caption,
)


# --------------------------------------------------------------------------- #
# pack_name_for_canva (RENDER form: uppercase, Pokemon-stripped)
# --------------------------------------------------------------------------- #


class TestPackNameForCanva:
    def test_strips_pokemon_word(self):
        assert pack_name_for_canva("Gold PSA 10 Pokemon Slab Pack") == "GOLD PSA 10 SLAB PACK"

    def test_strips_pokemon_accented(self):
        # "Pokémon" with the é accent — same whole-word match.
        assert pack_name_for_canva("Gold Pokémon Slab Pack") == "GOLD SLAB PACK"

    def test_case_insensitive_pokemon(self):
        assert pack_name_for_canva("gold POKEMON slab pack") == "GOLD SLAB PACK"
        assert pack_name_for_canva("Gold pokemon Slab Pack") == "GOLD SLAB PACK"

    def test_does_not_strip_pokemon_substring(self):
        # "POKEMONFOO" is not the word "Pokemon" — leave it alone.
        assert pack_name_for_canva("POKEMONFOO Pack") == "POKEMONFOO PACK"

    def test_collapses_double_space_after_strip(self):
        assert pack_name_for_canva("Gold  Pokemon  Slab  Pack") == "GOLD SLAB PACK"

    def test_preserves_punctuation(self):
        assert pack_name_for_canva("Mystic Mystery Mega Pack!") == "MYSTIC MYSTERY MEGA PACK!"

    def test_preserves_apostrophes(self):
        assert pack_name_for_canva("Don't Like It Raw") == "DON'T LIKE IT RAW"

    def test_empty_input_returns_empty(self):
        assert pack_name_for_canva("") == ""
        assert pack_name_for_canva(None) == ""

    def test_no_pokemon_word_unchanged_apart_from_case(self):
        assert pack_name_for_canva("Mega Stoned Pack") == "MEGA STONED PACK"

    def test_real_db_pack_names(self):
        # Real DripShopLive pack names observed in production.
        cases = [
            ("Gold PSA 10 Pokemon Slab Pack",       "GOLD PSA 10 SLAB PACK"),
            ("Silver Pokemon Slab Pack",            "SILVER SLAB PACK"),
            ("Starter Pokemon Slab Pack",           "STARTER SLAB PACK"),
            ("Mystic Mystery Dragonite Pack",       "MYSTIC MYSTERY DRAGONITE PACK"),
            ("Gengars Gone Wild Slab Pack",         "GENGARS GONE WILD SLAB PACK"),
            ("Don't like it Raw",                   "DON'T LIKE IT RAW"),
        ]
        for src, expected in cases:
            assert pack_name_for_canva(src) == expected, f"{src!r} → {pack_name_for_canva(src)!r}"


# --------------------------------------------------------------------------- #
# pack_name_for_caption (CAPTION form: title-cased, Pokemon-stripped)
# --------------------------------------------------------------------------- #


class TestPackNameForCaption:
    def test_strips_pokemon_and_title_cases(self):
        assert pack_name_for_caption("GOLD PSA 10 POKEMON SLAB PACK") == "Gold Psa 10 Slab Pack"

    def test_apostrophe_letter_lowercase(self):
        # "DON'T" → "Don't", not "Don'T"
        assert pack_name_for_caption("DON'T LIKE IT RAW") == "Don't Like It Raw"

    def test_preserves_exclamation(self):
        assert pack_name_for_caption("MYSTIC MYSTERY MEGA PACK!") == "Mystic Mystery Mega Pack!"

    def test_empty_input(self):
        assert pack_name_for_caption("") == ""
        assert pack_name_for_caption(None) == ""


# --------------------------------------------------------------------------- #
# card_name_cleanup (verbose grader-format → post-friendly)
# --------------------------------------------------------------------------- #


class TestCardNameCleanup:
    def test_psa_full_art_v(self):
        src = "PSA GEM MT 10 FA/GALARIAN MOLTRES V, 2021, POKEMON SWORD & SHIELD CHILLING REIGN, #177"
        assert card_name_cleanup(src) == "PSA 10 Galarian Moltres V"

    def test_psa_charizard_v(self):
        src = "PSA NM-MT 8 FA/CHARIZARD V, 2022, POKEMON SWSH BLACK STAR PROMO, #260"
        assert card_name_cleanup(src) == "PSA 8 Charizard V"

    def test_cgc_grader(self):
        src = "CGC 9 Single Strike Urshifu, 2021, Chilling Reign, #108/198"
        assert card_name_cleanup(src) == "CGC 9 Single Strike Urshifu"

    def test_psa_mint_basic(self):
        src = "PSA MINT 9 PIKACHU, 2026, POKEMON TEF EN-TEMPORAL FORCES, #51"
        assert card_name_cleanup(src) == "PSA 9 Pikachu"

    def test_psa_ex_lowercase_suffix(self):
        # "ex" stays lowercase per Pokemon TCG convention.
        src = "PSA GEM MT 10 SKELEDIRGE ex, 2023, POKEMON PAL EN-PALDEA EVOLVED, #258"
        assert card_name_cleanup(src) == "PSA 10 Skeledirge ex"

    def test_psa_gx_uppercase_suffix(self):
        src = "PSA GEM MT 10 STARMIE GX, 2019, POKEMON SUN & MOON HIDDEN FATES, #14"
        assert card_name_cleanup(src) == "PSA 10 Starmie GX"

    def test_holo_indicator_preserved(self):
        # Holo cards have a "-HOLO" suffix that we keep as-is.
        src = "PSA NM 7 METAL ENERGY-HOLO, 2000, POKEMON NEO GENESIS 1ST EDITION, #19"
        assert card_name_cleanup(src) == "PSA 7 Metal Energy-Holo"

    def test_apostrophe_in_card_name(self):
        # "Ethan's Magcargo" — apostrophe + post-apostrophe lowercase.
        src = "PSA GEM MT 10 ETHAN'S MAGCARGO, 2025, POKEMON JAPANESE M2a-MEGA DREAM ex, #197"
        assert card_name_cleanup(src) == "PSA 10 Ethan's Magcargo"

    def test_no_grader_passes_through(self):
        # Already-cleaned name shouldn't get mangled.
        src = "PSA 10 Galarian Moltres V"
        assert card_name_cleanup(src) == "PSA 10 Galarian Moltres V"

    def test_empty_input(self):
        assert card_name_cleanup("") == ""
        assert card_name_cleanup(None) == ""


# --------------------------------------------------------------------------- #
# format_price
# --------------------------------------------------------------------------- #


class TestFormatPrice:
    def test_whole_int(self):
        assert format_price(650) == "$650"

    def test_round_up(self):
        assert format_price(649.99) == "$650"

    def test_round_down(self):
        assert format_price(650.10) == "$650"

    def test_round_at_half(self):
        # Python's banker's rounding rounds .5 to even — 0.5 → 0, 1.5 → 2.
        # We don't depend on which way; just that something reasonable comes out.
        result = format_price(650.5)
        assert result in {"$650", "$651"}

    def test_string_input(self):
        assert format_price("1000.00") == "$1000"

    def test_zero(self):
        assert format_price(0) == "$0"

    def test_none_defensive(self):
        # Defensive — main.py should validate non-null first.
        assert format_price(None) == "$0"

    def test_invalid_string_defensive(self):
        assert format_price("not a number") == "$0"
