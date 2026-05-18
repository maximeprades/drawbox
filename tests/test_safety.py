"""Safety blocklist + please-detection."""

import pytest

from drawbox_core import has_please, is_safe


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
    assert has_please("a cat") is False


def test_has_please_french():
    assert has_please("un chat s'il te plait") is True
    assert has_please("un chat svp") is True


def test_has_please_handles_empty_input():
    assert has_please("") is False
    assert has_please(None) is False
