"""Tests for picking an occurrence's calendar location."""

from shared.locations import occurrence_location, schedule_location

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
