"""Safety blocklist + please-detection."""

import pytest

from drawbox_core import (
    contains_poop,
    has_please,
    is_safe,
    normalize_voice_command,
    parse_admin_poop_command,
)


@pytest.mark.parametrize("text", [
    "a happy dinosaur",
    "a kitty with a rainbow",
    "butterfly on a flower",          # contains "butter" — not blocked
    "grasshopper jumping",            # contains "grass" — not blocked
    "a class of fish",                # contains "ass" but only as substring
    "",
    "   ",
])
def test_is_safe_accepts_innocent(text):
    assert is_safe(text) is True


@pytest.mark.parametrize("text", [
    "kill it",
    "kill,it",                        # punctuation no longer hides the word
    "I want to KILL the dragon",      # case-insensitive
    "a gun and a knife",
    "naked man",
    "  death  ",
    "let's see blood",
])
def test_is_safe_rejects_blocked(text):
    assert is_safe(text) is False


def test_is_safe_does_not_match_substrings():
    # These would be false positives under naive substring matching, but
    # word-boundary tokenization keeps them safe.
    assert is_safe("a class of fish") is True
    assert is_safe("a butterfly") is True
    assert is_safe("the grass is green") is True
    assert is_safe("passionfruit") is True   # contains "ass" + "ion"


def test_has_please_english():
    assert has_please("a cat please") is True
    assert has_please("PLEASE draw a dog") is True
    assert has_please("draw me please a dragon") is True
    assert has_please("draw me a dragon, please!") is True
    assert has_please("can you please draw a cat") is True
    assert has_please("a cat") is False


def test_has_please_french():
    assert has_please("un chat s'il te plait") is True
    assert has_please("un chat s’il te plaît") is True
    assert has_please("un chat s il te plait") is True
    assert has_please("un chat sil vous plaît") is True
    assert has_please("un chat svp") is True


def test_has_please_handles_empty_input():
    assert has_please("") is False
    assert has_please(None) is False


@pytest.mark.parametrize("text", [
    "poop",
    "a car with poops on the roof",
    "a pooped puppy",
    "a pooping dinosaur",
    "a poopy unicorn",
    "POOP!",
])
def test_contains_poop_matches_poop_family(text):
    assert contains_poop(text) is True


@pytest.mark.parametrize("text", [
    "a poodle",
    "whoops",
    "a scoop of ice cream",
    "shampoo bottle",
    "",
    None,
])
def test_contains_poop_does_not_match_substrings(text):
    assert contains_poop(text) is False


def test_normalize_voice_command_strips_punctuation_and_case():
    assert normalize_voice_command(" Admin mode, ENABLE poop mode! ") == \
        "admin mode enable poop mode"


@pytest.mark.parametrize(("text", "expected"), [
    ("admin mode enable poop mode", "enable"),
    ("Admin mode, enable poop mode!", "enable"),
    (" admin   mode disable poop mode ", "disable"),
    ("admin mode disable poop mode.", "disable"),
])
def test_parse_admin_poop_command_exact_matches(text, expected):
    assert parse_admin_poop_command(text) == expected


@pytest.mark.parametrize("text", [
    "enable poop mode",
    "please enable poop mode",
    "admin enable poop mode",
    "admin mode enabled poop mode",
    "admin mode can you enable poop mode",
    "turn on poop mode",
])
def test_parse_admin_poop_command_rejects_near_misses(text):
    assert parse_admin_poop_command(text) is None
