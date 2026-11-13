"""Tests for CalendarClient's response logging, against a fake events() service."""

import json
import logging

from shared.calendar_client import CalendarClient


class _FakeRequest:
    def __init__(self, response):
        self._response = response

    def execute(self):
        return self._response


class _FakeEvents:
    def insert(self, calendarId, body):
        return _FakeRequest({"id": "evt1", **body})

    def patch(self, calendarId, eventId, body):
        return _FakeRequest({"id": eventId, **body})

    def delete(self, calendarId, eventId):
        return _FakeRequest("")


class _FakeService:
    def events(self):
        return _FakeEvents()


def _client():
    client = CalendarClient.__new__(CalendarClient)
    client.calendar_id = "cal"
    client._service = _FakeService()
    return client


def _response_lines(caplog):
    return [r.getMessage() for r in caplog.records if "GOOGLE_CALENDAR_RESPONSE" in r.getMessage()]


def test_logs_created_event_resource_verbatim(caplog):
    with caplog.at_level(logging.INFO):
        event = _client().create_event("Class", "2026-11-12T10:00:00", "2026-11-12T11:00:00")

    assert _response_lines(caplog) == [
        f"GOOGLE_CALENDAR_RESPONSE operation=insert event_id=None response={json.dumps(event)}"
    ]


def test_logs_patched_event_resource(caplog):
    with caplog.at_level(logging.INFO):
        _client().update_event("evt1", summary="Renamed")

    assert "operation=patch event_id=evt1" in _response_lines(caplog)[0]


def test_logs_delete_response(caplog):
    with caplog.at_level(logging.INFO):
        _client().delete_event("evt1")

    assert "operation=delete event_id=evt1" in _response_lines(caplog)[0]
