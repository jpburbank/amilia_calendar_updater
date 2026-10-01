"""Tests for the standalone backfill script's core logic."""

from datetime import datetime, timedelta, timezone

from backfill import (
    AUTHENTICATE_URL,
    _AmiliaRestClient,
    backfill_activity,
    backfill_facility_booking,
    backfill_program,
    build_activity_description,
    is_within_lookback,
    program_is_online,
)


class _FakeCalendarClient:
    def __init__(self):
        self.calls = []
        self._next_id = 0

    def create_event(self, **kwargs):
        self.calls.append(kwargs)
        self._next_id += 1
        return {"id": f"evt_{self._next_id}"}


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


class _FakeAmiliaResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeAmiliaSession:
    """Every URL returns one empty, fully-paged response; records what params were sent."""

    def __init__(self):
        self.calls = []

    def get(self, url, headers=None, auth=None, params=None):
        self.calls.append({"url": url, "params": params})
        if url == AUTHENTICATE_URL:
            return _FakeAmiliaResponse(200, {"Token": "tok"})
        return _FakeAmiliaResponse(200, {"Items": [], "Paging": {"TotalCount": 0, "Next": ""}})


_ONE_OCCURRENCE = [
    {"Id": 111, "Start": "2026-09-25T12:46:00-07:00", "End": "2026-09-25T13:46:00-07:00"}
]


def test_program_is_online_defaults_true_when_field_missing():
    assert program_is_online({"Id": 1, "Name": "No flag"}) is True


def test_program_is_online_respects_false():
    assert program_is_online({"Id": 1, "IsVisible": False}) is False


def test_within_lookback_keeps_future_activities_regardless_of_distance():
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    far_future = (datetime.now(timezone.utc) + timedelta(days=365 * 3)).isoformat()

    assert is_within_lookback({"EndDate": far_future}, cutoff) is True


def test_within_lookback_keeps_recently_ended_activities():
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()

    assert is_within_lookback({"EndDate": ten_days_ago}, cutoff) is True


def test_within_lookback_drops_activities_ended_long_ago():
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    a_year_ago = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()

    assert is_within_lookback({"EndDate": a_year_ago}, cutoff) is False


def test_within_lookback_keeps_activities_with_no_end_date():
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    assert is_within_lookback({}, cutoff) is True


def test_build_activity_description_all_three_fields():
    activity = {
        "Description": "<p>About the class.</p>",
        "Note": "Bring your own tools.",
        "Prerequisite": "Ages 10+",
    }

    assert build_activity_description(activity) == (
        "<p>About the class.</p>\n"
        "<p><strong>Note</strong><br>Bring your own tools.</p>\n"
        "<p><strong>Prerequisite</strong><br>Ages 10+</p>"
    )


def test_build_activity_description_omits_null_note_and_prerequisite():
    activity = {"Description": "<p>About the class.</p>", "Note": None, "Prerequisite": None}

    assert build_activity_description(activity) == "<p>About the class.</p>"


def test_build_activity_description_omits_empty_string_note_and_prerequisite():
    activity = {"Description": "<p>About the class.</p>", "Note": "", "Prerequisite": ""}

    assert build_activity_description(activity) == "<p>About the class.</p>"


def test_build_activity_description_none_when_nothing_present():
    assert build_activity_description({}) is None


def test_backfill_program_writes_document_and_returns_online():
    event_store = _FakeEventStore()

    online = backfill_program(
        event_store, {"Id": 107638, "Name": "Classes", "IsVisible": True}, dry_run=False
    )

    assert online is True
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
                "occurrences": {"111": {"start": "s", "end": "e", "calendar_event_id": "evt_existing"}}
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
            "summary": "Laser booking",
            "start_iso": "2026-09-08T11:45:00-07:00",
            "end_iso": "2026-09-08T12:00:00-07:00",
            "location": "Laser",
            "extended_properties": {"amilia_type": "FacilityBooking", "amilia_type_id": "FB-16905874"},
        }
    ]
    doc = event_store.get("FacilityBooking", "FB-16905874")
    assert doc["calendar_event_id"] == "evt_1"
    assert doc["action"] == "Create"


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


def test_get_programs_requests_the_default_page_size():
    client = _AmiliaRestClient("u", "p", session=_FakeAmiliaSession())

    client.get_programs(17659)

    call = next(c for c in client._session.calls if c["url"].endswith("/org/17659/programs"))
    assert call["params"]["perPage"] == 2000


def test_get_program_activities_requests_the_reduced_page_size():
    """Regression test: this endpoint's real max is 1000, not 2000 like the
    others — a perPage=2000 request 400s against the live API ("must be a
    whole number between 5 and 1000")."""
    client = _AmiliaRestClient("u", "p", session=_FakeAmiliaSession())

    client.get_program_activities(17659, 107638)

    call = next(c for c in client._session.calls if "activities" in c["url"])
    assert call["params"]["perPage"] == 1000


def test_get_activity_occurrences_requests_the_default_page_size():
    client = _AmiliaRestClient("u", "p", session=_FakeAmiliaSession())

    client.get_activity_occurrences(17659, 7311901)

    call = next(c for c in client._session.calls if "occurrences" in c["url"])
    assert call["params"]["perPage"] == 2000


def test_get_reservations_requests_the_default_page_size_and_date_range():
    client = _AmiliaRestClient("u", "p", session=_FakeAmiliaSession())

    client.get_reservations(17659, "2026-01-01", "2026-12-31")

    call = next(c for c in client._session.calls if "reservations" in c["url"])
    assert call["params"]["perPage"] == 2000
    assert call["params"]["from"] == "2026-01-01"
    assert call["params"]["to"] == "2026-12-31"
