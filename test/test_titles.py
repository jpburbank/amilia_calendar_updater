"""Tests for title normalization."""

import pytest

from shared.titles import clean_title


def test_strips_leading_and_trailing_whitespace():
    assert clean_title("  *Bandsaw Bear Claws  ") == "*Bandsaw Bear Claws"


def test_keeps_inner_whitespace():
    assert clean_title("Test - Bandsaw  Bear Claws") == "Test - Bandsaw  Bear Claws"


@pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
def test_missing_or_blank_title_becomes_default(value):
    assert clean_title(value, "Activity") == "Activity"


def test_missing_title_without_default_is_none():
    assert clean_title(None) is None
