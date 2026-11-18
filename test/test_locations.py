"""Tests for picking an occurrence's calendar location."""

import pytest

from shared.constants import GOOGLE_EVENT_COLOUR_IDS, LOCATION_COLOURS
from shared.locations import (
    event_colour_id,
    event_location,
    occurrence_location,
    schedule_location,
)

# Shapes as returned live by Amilia's REST API on 2026-11-14.
_OCCURRENCE = {"Id": 145865700, "Location": {"Id": 1923752, "Name": "Wood shop"}}
_ACTIVITY = {"Schedules": [{"Locations": [{"Id": 1923752, "Name": "Wood shop"}]}]}


def test_uses_the_occurrences_own_location_name():
    assert occurrence_location(_OCCURRENCE) == "Wood shop"


def test_occurrence_location_wins_over_the_schedule():
    activity = {"Schedules": [{"Locations": [{"Name": "Metal shop"}]}]}

    assert occurrence_location(_OCCURRENCE, activity) == "Wood shop"


def test_falls_back_to_the_activitys_schedule_locations():
    assert occurrence_location({"Id": 1, "Location": None}, _ACTIVITY) == "Wood shop"


def test_location_name_is_trimmed():
    assert occurrence_location({"Location": {"Name": "  Wood shop "}}) == "Wood shop"


def test_no_location_anywhere_is_none():
    assert occurrence_location({"Id": 1}, {"Schedules": []}) is None


def test_no_activity_and_no_occurrence_location_is_none():
    assert occurrence_location({"Id": 1}) is None


def test_schedule_location_joins_distinct_names_in_order():
    activity = {
        "Schedules": [
            {"Locations": [{"Name": "Wood shop"}, {"Name": "Metal shop"}]},
            {"Locations": [{"Name": "Wood shop"}, {"Name": " "}]},
        ]
    }

    assert schedule_location(activity) == "Wood shop, Metal shop"


def test_event_location_puts_the_room_before_the_building_address():
    assert event_location("Wood shop") == "Wood shop, 39449 Queens Way #1, Squamish, BC V8B 0R5"


def test_event_location_without_a_room_is_just_the_building_address():
    assert event_location(None) == "39449 Queens Way #1, Squamish, BC V8B 0R5"


def test_event_colour_id_maps_a_room_to_its_google_colour():
    assert event_colour_id("Pottery Studio") == "6"  # Tangerine


def test_event_colour_id_ignores_case_and_spacing():
    assert event_colour_id("  wood   SHOP ") == "5"  # Banana


@pytest.mark.parametrize("room", [None, "", "Rooftop"])
def test_event_colour_id_is_none_for_no_room_or_an_unlisted_room(room):
    assert event_colour_id(room) is None


def test_every_location_colour_is_a_google_colour():
    assert set(LOCATION_COLOURS.values()) <= set(GOOGLE_EVENT_COLOUR_IDS)
