"""Tests for the standalone backfill script's core logic."""

from datetime import UTC, datetime, timedelta

import pytest

from backfill import (
    backfill_activity,
    backfill_facility_booking,
    backfill_facility_bookings,
    backfill_program,
    is_within_lookback,
    program_is_online,
)
from shared.calendar_client import stable_event_id


class _FakeCalendarClient:
    def __init__(self, events=None):
        self.calls = []
        self.update_calls = []
        self.get_calls = []
        self.events = dict(events or {})  # {event_id: event resource}, for get_event
        self._next_id = 0

    def create_event(self, **kwargs):
        self.calls.append(kwargs)
        self._next_id += 1
        return {"id": f"evt_{self._next_id}"}

    def get_event(self, event_id):
        self.get_calls.append(event_id)
        return self.events.get(event_id)

    def update_event(self, event_id, **fields):
        self.update_calls.append((event_id, fields))
        return {"id": event_id}


class _FakeEventStore:
    def __init__(self, documents=None):
        self.documents = dict(documents or {})
        self.memberships = set()
        self.set_calls = []

    def get(self, context, external_id):
        doc = self.documents.get((context, external_id))
        return dict(doc) if doc is not None else None

    def set(self, context, external_id, fields, custom_time=None):
        self.documents[(context, external_id)] = dict(fields)
        self.set_calls.append((context, external_id, dict(fields), custom_time))

    def add_membership(self, parent_context, parent_id, child_context, child_id):
        self.memberships.add((parent_context, parent_id, child_context, child_id))


class _FakeAmiliaRestClient:
    def __init__(self, occurrences=None):
        self.occurrences = occurrences if occurrences is not None else []

    def get_activity_occurrences(self, org_id, activity_id):
        return self.occurrences


_ONE_OCCURRENCE = [
    {"Id": 111, "Start": "2026-09-25T12:46:00-07:00", "End": "2026-09-25T13:46:00-07:00"}
]


def test_program_is_online_defaults_true_when_field_missing():
    assert program_is_online({"Id": 1, "Name": "No flag"}) is True


def test_program_is_online_respects_false():
    assert program_is_online({"Id": 1, "IsVisible": False}) is False


def test_within_lookback_keeps_future_activities_regardless_of_distance():
    cutoff = datetime.now(UTC) - timedelta(days=30)
    far_future = (datetime.now(UTC) + timedelta(days=365 * 3)).isoformat()

    assert is_within_lookback({"EndDate": far_future}, cutoff) is True


def test_within_lookback_keeps_recently_ended_activities():
    cutoff = datetime.now(UTC) - timedelta(days=30)
    ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).isoformat()

    assert is_within_lookback({"EndDate": ten_days_ago}, cutoff) is True


def test_within_lookback_drops_activities_ended_long_ago():
    cutoff = datetime.now(UTC) - timedelta(days=30)
    a_year_ago = (datetime.now(UTC) - timedelta(days=365)).isoformat()

    assert is_within_lookback({"EndDate": a_year_ago}, cutoff) is False


def test_within_lookback_keeps_activities_with_no_end_date():
    cutoff = datetime.now(UTC) - timedelta(days=30)

    assert is_within_lookback({}, cutoff) is True


def test_backfill_program_writes_document_and_returns_online():
    event_store = _FakeEventStore()

    online = backfill_program(
        event_store, {"Id": 107638, "Name": "Classes", "IsVisible": True}, dry_run=False
    )

    assert online is True
    assert event_store.get("Program", 107638)["name"] == "Classes"


def test_backfill_program_trims_name_whitespace():
    event_store = _FakeEventStore()

    backfill_program(
        event_store, {"Id": 107638, "Name": " Classes  ", "IsVisible": True}, dry_run=False
    )

    assert event_store.get("Program", 107638)["name"] == "Classes"


def test_backfill_program_dry_run_writes_nothing():
    event_store = _FakeEventStore()

    backfill_program(event_store, {"Id": 107638, "IsVisible": True}, dry_run=True)

    assert event_store.get("Program", 107638) is None


def test_backfill_activity_creates_event_when_visible():
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore()
    amilia = _FakeAmiliaRestClient(occurrences=_ONE_OCCURRENCE)
    activity = {"Id": 7311901, "Name": "Test", "ProgramId": 107638, "Status": "Normal"}

    backfill_activity(
        calendar_client, event_store, amilia, 17659, activity, program_online=True, dry_run=False
    )

    assert len(calendar_client.calls) == 1
    doc = event_store.get("Activity", 7311901)
    assert doc["occurrences"]["111"]["calendar_event_id"] == "evt_1"
    assert ("Program", 107638, "Activities", 7311901) in event_store.memberships


def test_backfill_activity_trims_name_whitespace_for_calendar_and_store():
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore()
    amilia = _FakeAmiliaRestClient(occurrences=_ONE_OCCURRENCE)
    activity = {"Id": 7311901, "Name": "  Test  ", "ProgramId": 107638, "Status": "Normal"}

    backfill_activity(
        calendar_client, event_store, amilia, 17659, activity, program_online=True, dry_run=False
    )

    assert calendar_client.calls[0]["summary"] == "Test"
    assert event_store.get("Activity", 7311901)["name"] == "Test"


def test_backfill_activity_event_location_comes_from_the_occurrence():
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore()
    occurrences = [{**_ONE_OCCURRENCE[0], "Location": {"Id": 1923752, "Name": "Wood shop"}}]
    amilia = _FakeAmiliaRestClient(occurrences=occurrences)
    activity = {"Id": 7311901, "Name": "Test", "ProgramId": 107638, "Status": "Normal"}

    backfill_activity(
        calendar_client, event_store, amilia, 17659, activity, program_online=True, dry_run=False
    )

    assert calendar_client.calls[0]["location"] == "Wood shop"
    assert event_store.get("Activity", 7311901)["occurrences"]["111"]["location"] == "Wood shop"


def test_backfill_activity_location_falls_back_to_schedules():
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore()
    amilia = _FakeAmiliaRestClient(occurrences=_ONE_OCCURRENCE)
    activity = {
        "Id": 7311901,
        "Name": "Test",
        "ProgramId": 107638,
        "Status": "Normal",
        "Schedules": [{"Locations": [{"Name": "Metal shop"}]}],
    }

    backfill_activity(
        calendar_client, event_store, amilia, 17659, activity, program_online=True, dry_run=False
    )

    assert calendar_client.calls[0]["location"] == "Metal shop"


def test_backfill_activity_sets_amilia_type_extended_properties():
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore()
    amilia = _FakeAmiliaRestClient(occurrences=_ONE_OCCURRENCE)
    activity = {"Id": 7311901, "Name": "Test", "ProgramId": 107638, "Status": "Normal"}

    backfill_activity(
        calendar_client, event_store, amilia, 17659, activity, program_online=True, dry_run=False
    )

    assert calendar_client.calls[0]["extended_properties"] == {
        "amilia_type": "Activity",
        "amilia_type_id": "7311901",
    }


def test_backfill_activity_passes_description_through_to_calendar():
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore()
    amilia = _FakeAmiliaRestClient(occurrences=_ONE_OCCURRENCE)
    activity = {
        "Id": 7311901,
        "Name": "Test",
        "ProgramId": 107638,
        "Status": "Normal",
        "Description": "<p>About the class.</p>",
        "Note": "Bring your own tools.",
        "Prerequisite": None,
    }

    backfill_activity(
        calendar_client, event_store, amilia, 17659, activity, program_online=True, dry_run=False
    )

    assert calendar_client.calls[0]["description"] == (
        "<p>About the class.</p>\n<p><strong>Note</strong><br>Bring your own tools.</p>"
    )


def test_backfill_activity_skips_calendar_when_program_offline():
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore()
    amilia = _FakeAmiliaRestClient(occurrences=_ONE_OCCURRENCE)
    activity = {"Id": 7311901, "Name": "Test", "ProgramId": 107638, "Status": "Normal"}

    backfill_activity(
        calendar_client, event_store, amilia, 17659, activity, program_online=False, dry_run=False
    )

    assert calendar_client.calls == []
    doc = event_store.get("Activity", 7311901)
    assert doc["occurrences"]["111"]["calendar_event_id"] is None


def test_backfill_activity_skips_calendar_when_status_not_normal():
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore()
    amilia = _FakeAmiliaRestClient(occurrences=_ONE_OCCURRENCE)
    activity = {"Id": 7311901, "Name": "Test", "ProgramId": 107638, "Status": "Hidden"}

    backfill_activity(
        calendar_client, event_store, amilia, 17659, activity, program_online=True, dry_run=False
    )

    assert calendar_client.calls == []


def test_backfill_activity_is_idempotent_on_rerun():
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore(
        documents={
            ("Activity", 7311901): {
                "occurrences": {
                    "111": {"start": "s", "end": "e", "calendar_event_id": "evt_existing"}
                }
            }
        }
    )
    amilia = _FakeAmiliaRestClient(occurrences=_ONE_OCCURRENCE)
    activity = {"Id": 7311901, "Name": "Test", "ProgramId": 107638, "Status": "Normal"}

    backfill_activity(
        calendar_client, event_store, amilia, 17659, activity, program_online=True, dry_run=False
    )

    assert calendar_client.calls == []  # already had a calendar_event_id — not recreated
    doc = event_store.get("Activity", 7311901)
    assert doc["occurrences"]["111"]["calendar_event_id"] == "evt_existing"


def test_backfill_activity_dry_run_writes_nothing():
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore()
    amilia = _FakeAmiliaRestClient(occurrences=_ONE_OCCURRENCE)
    activity = {"Id": 7311901, "Name": "Test", "ProgramId": 107638, "Status": "Normal"}

    backfill_activity(
        calendar_client, event_store, amilia, 17659, activity, program_online=True, dry_run=True
    )

    assert calendar_client.calls == []
    assert event_store.get("Activity", 7311901) is None
    assert event_store.memberships == set()


_RESERVATION = {
    "ReservationId": "FB-16905874",
    "Title": "Laser",
    "Type": "FacilityBooking",
    "Start": "2026-09-08T11:45:00-07:00",
    "End": "2026-09-08T12:00:00-07:00",
    "IsCancelled": False,
    "Location": {"Id": 1923754, "Name": "Laser"},
}


def test_backfill_facility_booking_creates_event_when_new():
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore()

    backfill_facility_booking(calendar_client, event_store, _RESERVATION, dry_run=False)

    assert calendar_client.calls == [
        {
            "event_id": stable_event_id("FacilityBooking", "FB-16905874"),
            "summary": "Laser booking",
            "start_iso": "2026-09-08T11:45:00-07:00",
            "end_iso": "2026-09-08T12:00:00-07:00",
            "location": "Laser",
            "extended_properties": {
                "amilia_type": "FacilityBooking",
                "amilia_type_id": "FB-16905874",
            },
        }
    ]
    doc = event_store.get("FacilityBooking", "FB-16905874")
    assert doc["calendar_event_id"] == "evt_1"
    assert doc["action"] == "Create"


def test_backfill_facility_booking_trims_title_whitespace():
    calendar_client = _FakeCalendarClient()
    reservation = {**_RESERVATION, "Title": "  Laser "}

    backfill_facility_booking(calendar_client, _FakeEventStore(), reservation, dry_run=False)

    assert calendar_client.calls[0]["summary"] == "Laser booking"


@pytest.mark.parametrize("title", [None, "", "   "])
def test_backfill_facility_booking_uses_default_title_when_title_is_blank(title):
    """Amilia sends Title as null/empty, not missing, on untitled admin bookings."""
    calendar_client = _FakeCalendarClient()
    reservation = {**_RESERVATION, "Title": title}

    backfill_facility_booking(calendar_client, _FakeEventStore(), reservation, dry_run=False)

    assert calendar_client.calls[0]["summary"] == "Facility Booking booking"


def test_backfill_facility_booking_skips_cancelled_reservation():
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore()
    reservation = {**_RESERVATION, "IsCancelled": True}

    backfill_facility_booking(calendar_client, event_store, reservation, dry_run=False)

    assert calendar_client.calls == []
    assert event_store.get("FacilityBooking", "FB-16905874") is None


def test_backfill_facility_booking_skips_type_activity_reservation():
    """Regression test: Type=="Activity" reservations are booked sessions of
    an Activity, already covered by backfill_activity() — importing them
    here too duplicated every Activity occurrence's calendar event in
    production (confirmed: 251 of 457 FacilityBooking docs had an AC-
    prefix, each a duplicate)."""
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore()
    reservation = {**_RESERVATION, "ReservationId": "AC-138583648", "Type": "Activity"}

    backfill_facility_booking(calendar_client, event_store, reservation, dry_run=False)

    assert calendar_client.calls == []
    assert event_store.get("FacilityBooking", "AC-138583648") is None


def test_backfill_facility_booking_is_idempotent_on_rerun():
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore(
        documents={("FacilityBooking", "FB-16905874"): {"calendar_event_id": "evt_existing"}}
    )

    backfill_facility_booking(calendar_client, event_store, _RESERVATION, dry_run=False)

    assert calendar_client.calls == []  # already tracked — not recreated
    doc = event_store.get("FacilityBooking", "FB-16905874")
    assert doc["calendar_event_id"] == "evt_existing"


def test_backfill_facility_booking_dry_run_writes_nothing():
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore()

    backfill_facility_booking(calendar_client, event_store, _RESERVATION, dry_run=True)

    assert calendar_client.calls == []
    assert event_store.get("FacilityBooking", "FB-16905874") is None


class _FakeReservationsClient:
    def __init__(self, reservations):
        self.reservations = reservations
        self.calls = []

    def get_reservations(self, org_id, from_date, to_date):
        self.calls.append((org_id, from_date, to_date))
        return self.reservations


def test_backfill_facility_bookings_does_nothing_when_disabled():
    amilia = _FakeReservationsClient([dict(_RESERVATION)])
    calendar = _FakeCalendarClient()
    failed = backfill_facility_bookings(
        calendar,
        _FakeEventStore(),
        amilia,
        17659,
        "2026-01-01",
        "2026-12-31",
        enabled=False,
        dry_run=False,
    )
    assert failed == []
    assert amilia.calls == []
    assert calendar.calls == []


def test_backfill_facility_bookings_imports_reservations_when_enabled(monkeypatch):
    monkeypatch.setattr("backfill.REQUEST_PACING_SECONDS", 0)
    amilia = _FakeReservationsClient([dict(_RESERVATION)])
    calendar = _FakeCalendarClient()
    failed = backfill_facility_bookings(
        calendar,
        _FakeEventStore(),
        amilia,
        17659,
        "2026-01-01",
        "2026-12-31",
        enabled=True,
        dry_run=False,
    )
    assert failed == []
    assert amilia.calls == [(17659, "2026-01-01", "2026-12-31")]
    assert len(calendar.calls) == 1


def test_backfill_activity_creates_events_under_stable_ids():
    """A rerun that died midway can't duplicate events."""
    calendar_client = _FakeCalendarClient()
    amilia = _FakeAmiliaRestClient(occurrences=_ONE_OCCURRENCE)
    activity = {"Id": 7311901, "Name": "Test", "ProgramId": 107638, "Status": "Normal"}

    backfill_activity(
        calendar_client,
        _FakeEventStore(),
        amilia,
        17659,
        activity,
        program_online=True,
        dry_run=False,
    )

    assert calendar_client.calls[0]["event_id"] == stable_event_id("Activity", 7311901, "111")


# ---------------------------------------------------------------------------
# --update-existing
# ---------------------------------------------------------------------------

_LOCATED_OCCURRENCE = [{**_ONE_OCCURRENCE[0], "Location": {"Name": "Wood shop"}}]
_VISIBLE_ACTIVITY = {"Id": 7311901, "Name": "Test", "ProgramId": 107638, "Status": "Normal"}


def _tracked_store(**record):
    return _FakeEventStore(
        documents={
            ("Activity", 7311901): {
                "occurrences": {"111": {"calendar_event_id": "evt_old"}},
                **record,
            }
        }
    )


def _calendar_event(**overrides):
    """evt_old as the calendar returns it: times in UTC, no location yet."""
    event = {
        "id": "evt_old",
        "status": "confirmed",
        "summary": "Test",
        "start": {"dateTime": "2026-09-25T19:46:00Z", "timeZone": "UTC"},
        "end": {"dateTime": "2026-09-25T20:46:00Z", "timeZone": "UTC"},
        "extendedProperties": {"private": {"amilia_type": "Activity", "amilia_type_id": "7311901"}},
    }
    event.update(overrides)
    return event


def _backfill(calendar_client, event_store, *, update_existing, dry_run=False, **activity):
    backfill_activity(
        calendar_client,
        event_store,
        _FakeAmiliaRestClient(occurrences=_LOCATED_OCCURRENCE),
        17659,
        {**_VISIBLE_ACTIVITY, **activity},
        program_online=True,
        dry_run=dry_run,
        update_existing=update_existing,
    )


def test_existing_events_are_left_alone_by_default():
    calendar_client = _FakeCalendarClient(events={"evt_old": _calendar_event()})

    _backfill(calendar_client, _tracked_store(), update_existing=False)

    assert (calendar_client.get_calls, calendar_client.update_calls) == ([], [])


def test_update_existing_patches_only_the_fields_that_differ():
    calendar_client = _FakeCalendarClient(events={"evt_old": _calendar_event()})

    _backfill(calendar_client, _tracked_store(), update_existing=True)

    assert calendar_client.update_calls == [("evt_old", {"location": "Wood shop"})]


def test_update_existing_leaves_matching_events_unpatched():
    calendar_client = _FakeCalendarClient(events={"evt_old": _calendar_event(location="Wood shop")})

    _backfill(calendar_client, _tracked_store(), update_existing=True)

    assert calendar_client.update_calls == []


def test_update_existing_dry_run_reads_and_prints_but_writes_nothing(capsys):
    calendar_client = _FakeCalendarClient(events={"evt_old": _calendar_event()})
    event_store = _tracked_store()

    _backfill(calendar_client, event_store, update_existing=True, dry_run=True)

    assert calendar_client.update_calls == []
    assert event_store.set_calls == []
    assert "event evt_old location: None -> 'Wood shop'" in capsys.readouterr().out


@pytest.mark.parametrize("event", [None, _calendar_event(status="cancelled")])
def test_update_existing_never_recreates_a_missing_or_cancelled_event(event):
    calendar_client = _FakeCalendarClient(events={"evt_old": event} if event else {})

    _backfill(calendar_client, _tracked_store(), update_existing=True)

    assert (calendar_client.calls, calendar_client.update_calls) == ([], [])


def test_update_existing_skips_hidden_activities():
    calendar_client = _FakeCalendarClient(events={"evt_old": _calendar_event()})

    _backfill(calendar_client, _tracked_store(), update_existing=True, Status="Hidden")

    assert (calendar_client.get_calls, calendar_client.update_calls) == ([], [])


def test_backfill_keeps_the_stored_last_event_time():
    """Dropping it would make the next replayed old webhook look fresh."""
    event_store = _tracked_store(last_event_time="2026-11-14T15:04:06-05:00")

    _backfill(_FakeCalendarClient(), event_store, update_existing=False)

    stored = event_store.get("Activity", 7311901)
    assert stored["last_event_time"] == "2026-11-14T15:04:06-05:00"


def test_backfill_stores_the_description_for_the_reconciler():
    event_store = _FakeEventStore()

    _backfill(
        _FakeCalendarClient(), event_store, update_existing=False, Description="<p>About.</p>"
    )

    assert event_store.get("Activity", 7311901)["description"] == "<p>About.</p>"
