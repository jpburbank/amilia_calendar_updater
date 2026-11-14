"""Tests for CalendarClient's idempotent create and response logging, against fakes."""

import json
import logging
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

    def delete(self, calendarId, eventId):
        return _FakeRequest("")


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


def _response_lines(caplog):
    return [r.getMessage() for r in caplog.records if "GOOGLE_CALENDAR_RESPONSE" in r.getMessage()]


def test_logs_created_event_resource_verbatim(caplog):
    with caplog.at_level(logging.INFO):
        event = _client(_FakeEvents()).create_event(**_ARGS)

    assert _response_lines(caplog) == [
        f"GOOGLE_CALENDAR_RESPONSE operation=insert event_id=None response={json.dumps(event)}"
    ]


def test_logs_restoring_patch_when_stable_id_exists(caplog):
    with caplog.at_level(logging.INFO):
        _client(_FakeEvents(insert_status=409)).create_event(**_ARGS, event_id="abc123")

    assert "operation=patch event_id=abc123" in _response_lines(caplog)[0]


def test_logs_patched_event_resource(caplog):
    with caplog.at_level(logging.INFO):
        _client(_FakeEvents()).update_event("evt1", summary="Renamed")

    assert "operation=patch event_id=evt1" in _response_lines(caplog)[0]


def test_logs_delete_response(caplog):
    with caplog.at_level(logging.INFO):
        _client(_FakeEvents()).delete_event("evt1")

    assert "operation=delete event_id=evt1" in _response_lines(caplog)[0]
