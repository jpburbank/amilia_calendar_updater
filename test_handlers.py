"""Tests for per-context webhook handlers."""

from handlers import handle_facility_booking


class _FakeCalendarClient:
    def __init__(self):
        self.calls = []

    def create_event(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": "evt_123"}


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
        action="Create", payload=_booking_payload(Title="Laser"), calendar_client=calendar_client
    )

    assert calendar_client.calls[0]["summary"] == "Laser booking"


def test_facility_booking_create_appends_booking_to_default_title():
    calendar_client = _FakeCalendarClient()

    handle_facility_booking(
        action="Create", payload=_booking_payload(), calendar_client=calendar_client
    )

    assert calendar_client.calls[0]["summary"] == "Facility Booking booking"
