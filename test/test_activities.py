"""Tests for what an Activity's stored record and calendar events contain."""

from shared.activities import (
    activity_event_fields,
    activity_record,
    build_activity_description,
    stored_occurrence,
)

# A REST Activity, trimmed to the fields that matter (shape as seen live 2026-11-14).
_ACTIVITY = {
    "Id": 7485693,
    "Name": "Test - Bandsaw Bear Claws  ",
    "ProgramId": 107638,
    "CategoryId": 5525740,
    "CategoryName": "Wood Shop",
    "SubCategoryId": 5525742,
    "SubCategoryName": "Project Classes",
    "Status": "Normal",
    "StartDate": "2027-01-01T09:30:00-07:00",
    "EndDate": "2027-01-01T12:30:00-07:00",
    "Description": "<p>About the class.</p>",
    "Schedules": [{"Locations": [{"Id": 1923752, "Name": "Wood shop"}]}],
}
_OCCURRENCE = {
    "Id": 145865700,
    "Start": "2027-01-01T09:30:00-07:00",
    "End": "2027-01-01T12:30:00-07:00",
    "Location": {"Id": 1923752, "Name": "Wood shop"},
}


def test_build_activity_description_all_fields_in_order():
    activity = {
        "Description": "<p>About the class.</p>",
        "Note": "Bring your own tools.",
        "Prerequisite": "Ages 10+",
        "AdditionalInformation": "Parking at the back.",
        "ResponsibleName": "Sam Lee",
    }

    assert build_activity_description(activity) == (
        "<p>About the class.</p>\n"
        "<p><strong>Note</strong><br>Bring your own tools.</p>\n"
        "<p><strong>Prerequisite</strong><br>Ages 10+</p>\n"
        "<p><strong>Additional Information</strong><br>Parking at the back.</p>\n"
        "<p><strong>Contact person at organization</strong><br>Sam Lee</p>"
    )


def test_build_activity_description_omits_null_headed_fields():
    activity = {
        "Description": "<p>About the class.</p>",
        "Note": None,
        "Prerequisite": None,
        "AdditionalInformation": None,
        "ResponsibleName": None,
    }

    assert build_activity_description(activity) == "<p>About the class.</p>"


def test_build_activity_description_omits_empty_string_headed_fields():
    activity = {
        "Description": "<p>About the class.</p>",
        "Note": "",
        "Prerequisite": "",
        "AdditionalInformation": "",
        "ResponsibleName": "",
    }

    assert build_activity_description(activity) == "<p>About the class.</p>"


def test_build_activity_description_header_only_field_without_description():
    activity = {"Description": None, "ResponsibleName": "Sam Lee"}

    assert build_activity_description(activity) == (
        "<p><strong>Contact person at organization</strong><br>Sam Lee</p>"
    )


def test_build_activity_description_none_when_nothing_present():
    assert build_activity_description({}) is None


def test_activity_record_holds_what_events_need():
    record = activity_record(_ACTIVITY, "Normal")

    assert record == {
        "name": "Test - Bandsaw Bear Claws",
        "program_id": 107638,
        "category_id": 5525740,
        "category_name": "Wood Shop",
        "sub_category_id": 5525742,
        "sub_category_name": "Project Classes",
        "status": "Normal",
        "start_date": "2027-01-01T09:30:00-07:00",
        "end_date": "2027-01-01T12:30:00-07:00",
        "description": "<p>About the class.</p>",
        "location": "Wood shop",
    }


def test_activity_record_treats_program_zero_as_none():
    assert activity_record({**_ACTIVITY, "ProgramId": 0}, "Normal")["program_id"] is None


def test_stored_occurrence_keeps_times_and_location():
    assert stored_occurrence(_OCCURRENCE, _ACTIVITY) == {
        "start": "2027-01-01T09:30:00-07:00",
        "end": "2027-01-01T12:30:00-07:00",
        "location": "Wood shop",
    }


def test_activity_event_fields_from_record_and_occurrence():
    record = activity_record(_ACTIVITY, "Normal")

    fields = activity_event_fields(7485693, record, stored_occurrence(_OCCURRENCE, _ACTIVITY))

    assert fields == {
        "summary": "Test - Bandsaw Bear Claws",
        "start_iso": "2027-01-01T09:30:00-07:00",
        "end_iso": "2027-01-01T12:30:00-07:00",
        "location": "Wood shop",
        "description": "<p>About the class.</p>",
        "extended_properties": {"amilia_type": "Activity", "amilia_type_id": "7485693"},
    }


def test_activity_event_fields_fall_back_to_record_location_and_default_title():
    """Records stored before per-occurrence locations, or with a blank name."""
    record = {"name": None, "location": "Metal shop"}
    occurrence = {"start": "a", "end": "b"}

    fields = activity_event_fields(1, record, occurrence)

    assert (fields["summary"], fields["location"], fields["description"]) == (
        "Activity",
        "Metal shop",
        None,
    )
