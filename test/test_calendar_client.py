"""Tests for CalendarClient's idempotent create, against a fake events() service."""

import re

import pytest
from googleapiclient.errors import HttpError

from shared.calendar_client import CalendarClient, stable_event_id


class _Resp(dict):
    def __init__(self, status):
        super().__init__(status=str(status))
        self.status = status
        self.reason = ""


class _FakeRequest:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def execute(self):
        if self._error:
            raise self._error
        return self._result


class _FakeEvents:
    def __init__(self, insert_status=200):
        self.insert_status = insert_status
        self.inserted = []
        self.patched = []

    def insert(self, calendarId, body):
        self.inserted.append(body)
        if self.insert_status != 200:
            return _FakeRequest(error=HttpError(_Resp(self.insert_status), b"{}"))
        return _FakeRequest({"id": body.get("id", "random"), **body})

    def patch(self, calendarId, eventId, body):
        self.patched.append((eventId, body))
        return _FakeRequest({"id": eventId, **body})


class _FakeService:
    def __init__(self, events):
        self._events = events

    def events(self):
        return self._events


def _client(events):
    client = CalendarClient.__new__(CalendarClient)
    client.calendar_id = "cal"
    client._service = _FakeService(events)
    return client


_ARGS = {"summary": "Class", "start_iso": "2027-01-01T09:30:00-07:00", "end_iso": "x"}


def test_stable_event_id_is_valid_base32hex():
    event_id = stable_event_id("Activity", 7482865, 145842706)

    assert re.fullmatch(r"[0-9a-v]{5,1024}", event_id)


def test_stable_event_id_is_stable_and_distinct():
    assert stable_event_id("Activity", 1, 2) == stable_event_id("Activity", "1", "2")
    assert stable_event_id("Activity", 1, 2) != stable_event_id("Activity", 12)


def test_create_with_event_id_inserts_under_that_id():
    events = _FakeEvents()

    event = _client(events).create_event(**_ARGS, event_id="abc123")

    assert events.inserted[0]["id"] == "abc123"
    assert event["id"] == "abc123"
    assert events.patched == []


def test_create_with_existing_event_id_patches_and_confirms_instead():
    """A retried create, or an event deleted earlier (Calendar keeps it as "cancelled")."""
    events = _FakeEvents(insert_status=409)

    event = _client(events).create_event(**_ARGS, event_id="abc123")

    assert event["id"] == "abc123"
    event_id, body = events.patched[0]
    assert event_id == "abc123"
    assert body["status"] == "confirmed"
    assert body["summary"] == "Class"


def test_create_with_event_id_propagates_other_errors():
    events = _FakeEvents(insert_status=403)

    with pytest.raises(HttpError):
        _client(events).create_event(**_ARGS, event_id="abc123")

    assert events.patched == []


def test_create_without_event_id_lets_calendar_pick_one():
    events = _FakeEvents()

    _client(events).create_event(**_ARGS)

    assert "id" not in events.inserted[0]
