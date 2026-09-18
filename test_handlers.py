"""Tests for per-context webhook handlers."""

from handlers import handle_facility_booking


class _FakeCalendarClient:
    def __init__(self):
        self.calls = []
        self.update_calls = []
        self.delete_calls = []

    def create_event(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": "evt_123"}

    def update_event(self, event_id, **fields):
        self.update_calls.append((event_id, fields))
        return {"id": event_id}

    def delete_event(self, event_id):
        self.delete_calls.append(event_id)


class _FakeEventStore:
    def __init__(self, initial=None):
        self.mappings = dict(initial or {})  # {(context, external_id): event_id}
        self.set_calls = []
        self.delete_calls = []

    def get(self, context, external_id):
        return self.mappings.get((context, external_id))

    def set(self, context, external_id, event_id, action, end_iso):
        self.mappings[(context, external_id)] = event_id
        self.set_calls.append((context, external_id, event_id, action, end_iso))

    def delete(self, context, external_id):
        self.mappings.pop((context, external_id), None)
        self.delete_calls.append((context, external_id))


def _booking_payload(**overrides):
    payload = {
        "ReservationId": "FB-1",
        "Start": "2026-01-01T10:00:00",
        "End": "2026-01-01T11:00:00",
    }
    payload.update(overrides)
    return payload


def test_facility_booking_create_appends_booking_to_title():
    calendar_client = _FakeCalendarClient()

    handle_facility_booking(
        action="Create",
        payload=_booking_payload(Title="Laser"),
        calendar_client=calendar_client,
        event_store=_FakeEventStore(),
    )

    assert calendar_client.calls[0]["summary"] == "Laser booking"


def test_facility_booking_create_appends_booking_to_default_title():
    calendar_client = _FakeCalendarClient()

    handle_facility_booking(
        action="Create",
        payload=_booking_payload(),
        calendar_client=calendar_client,
        event_store=_FakeEventStore(),
    )

    assert calendar_client.calls[0]["summary"] == "Facility Booking booking"


def test_facility_booking_create_stores_mapping():
    event_store = _FakeEventStore()

    handle_facility_booking(
        action="Create",
        payload=_booking_payload(),
        calendar_client=_FakeCalendarClient(),
        event_store=event_store,
    )

    assert event_store.mappings[("FacilityBooking", "FB-1")] == "evt_123"
    assert event_store.set_calls == [
        ("FacilityBooking", "FB-1", "evt_123", "Create", "2026-01-01T11:00:00")
    ]


def test_facility_booking_update_uses_stored_mapping():
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore(initial={("FacilityBooking", "FB-1"): "evt_123"})

    result = handle_facility_booking(
        action="Update",
        payload=_booking_payload(Title="Laser"),
        calendar_client=calendar_client,
        event_store=event_store,
    )

    assert result == {
        "reservation_id": "FB-1",
        "calendar_event_id": "evt_123",
        "updated": True,
    }
    event_id, fields = calendar_client.update_calls[0]
    assert event_id == "evt_123"
    assert fields["summary"] == "Laser"


def test_facility_booking_update_with_no_mapping_skips():
    calendar_client = _FakeCalendarClient()

    result = handle_facility_booking(
        action="Update",
        payload=_booking_payload(),
        calendar_client=calendar_client,
        event_store=_FakeEventStore(),
    )

    assert result == {"reservation_id": "FB-1", "skipped": "no stored event mapping"}
    assert calendar_client.update_calls == []


def test_facility_booking_delete_removes_mapping_and_calendar_event():
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore(initial={("FacilityBooking", "FB-1"): "evt_123"})

    result = handle_facility_booking(
        action="Delete",
        payload={"Id": "FB-1"},
        calendar_client=calendar_client,
        event_store=event_store,
    )

    assert result == {"reservation_id": "FB-1", "deleted": True}
    assert calendar_client.delete_calls == ["evt_123"]
    assert ("FacilityBooking", "FB-1") not in event_store.mappings


def test_facility_booking_delete_with_no_mapping_skips():
    calendar_client = _FakeCalendarClient()

    result = handle_facility_booking(
        action="Delete",
        payload={"Id": "FB-1"},
        calendar_client=calendar_client,
        event_store=_FakeEventStore(),
    )

    assert result == {"reservation_id": "FB-1", "skipped": "no stored event mapping"}
    assert calendar_client.delete_calls == []
