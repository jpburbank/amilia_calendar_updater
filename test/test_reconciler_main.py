"""Tests for the Cloud Tasks reconciliation worker."""

import os

os.environ.setdefault("GOOGLE_CALENDAR_ID", "test-calendar@group.calendar.google.com")
os.environ.setdefault("GOOGLE_EVENT_STORE_BUCKET", "test-bucket")

from flask import Flask, request  # noqa: E402

# Both src/webhook/main.py and src/reconciler/main.py are literally named
# main.py (a Cloud Functions constraint — see either module's docstring),
# so loading this one by explicit file path with a distinct name avoids a
# sys.modules collision with test_webhook_main.py in the same pytest
# session — see that file's comment for the full explanation.
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "reconciler_main", Path(__file__).parent.parent / "src" / "reconciler" / "main.py"
)
reconciler_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reconciler_main)  # noqa: E402  (import needs the env vars above set)

app = Flask(__name__)


class _FakeCalendarClient:
    def __init__(self):
        self.calls = []
        self.delete_calls = []

    def create_event(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": "evt_new"}

    def delete_event(self, event_id):
        self.delete_calls.append(event_id)


class _FakeEventStore:
    def __init__(self, documents=None):
        self.documents = dict(documents or {})

    def get(self, context, external_id):
        doc = self.documents.get((context, external_id))
        return dict(doc) if doc is not None else None

    def set(self, context, external_id, fields, custom_time=None):
        self.documents[(context, external_id)] = dict(fields)


def _call(activity_id, monkeypatch, calendar_client, event_store):
    monkeypatch.setattr(reconciler_main, "_get_calendar_client", lambda: calendar_client)
    monkeypatch.setattr(reconciler_main, "_get_event_store", lambda: event_store)
    with app.test_request_context(method="POST", json={"activity_id": activity_id}):
        response, status_code = reconciler_main.reconcile_activity(request)
        return status_code, response.get_json()


def test_missing_activity_id_is_a_400(monkeypatch):
    status_code, body = _call(None, monkeypatch, _FakeCalendarClient(), _FakeEventStore())

    assert status_code == 400


def test_unknown_activity_is_skipped(monkeypatch):
    status_code, body = _call("111", monkeypatch, _FakeCalendarClient(), _FakeEventStore())

    assert status_code == 200
    assert body["skipped"] == "no stored activity"


def test_program_now_online_creates_missing_calendar_event(monkeypatch):
    event_store = _FakeEventStore(
        documents={
            ("Program", 107638): {"online": True},
            ("Activity", "111"): {
                "program_id": 107638,
                "status": "Normal",
                "name": "Test Activity",
                "location": "Metal shop",
                "end_date": "2026-10-27T13:29:00-04:00",
                "occurrences": {
                    "a": {
                        "start": "2026-09-25T12:46:00-07:00",
                        "end": "2026-09-25T13:46:00-07:00",
                        "calendar_event_id": None,
                    }
                },
            },
        }
    )
    calendar_client = _FakeCalendarClient()

    status_code, body = _call("111", monkeypatch, calendar_client, event_store)

    assert status_code == 200
    assert body["visible"] is True
    assert body["changed"] is True
    assert calendar_client.calls[0]["location"] == "Metal shop"
    assert (
        event_store.documents[("Activity", "111")]["occurrences"]["a"]["calendar_event_id"]
        == "evt_new"
    )


def test_program_now_offline_deletes_existing_calendar_event(monkeypatch):
    event_store = _FakeEventStore(
        documents={
            ("Program", 107638): {"online": False},
            ("Activity", "111"): {
                "program_id": 107638,
                "status": "Normal",
                "occurrences": {"a": {"start": "s", "end": "e", "calendar_event_id": "evt_old"}},
            },
        }
    )
    calendar_client = _FakeCalendarClient()

    status_code, body = _call("111", monkeypatch, calendar_client, event_store)

    assert body["visible"] is False
    assert body["changed"] is True
    assert calendar_client.delete_calls == ["evt_old"]
    assert (
        event_store.documents[("Activity", "111")]["occurrences"]["a"]["calendar_event_id"] is None
    )


def test_already_matching_state_is_a_noop(monkeypatch):
    event_store = _FakeEventStore(
        documents={
            ("Program", 107638): {"online": True},
            ("Activity", "111"): {
                "program_id": 107638,
                "status": "Normal",
                "occurrences": {
                    "a": {"start": "s", "end": "e", "calendar_event_id": "evt_existing"}
                },
            },
        }
    )
    calendar_client = _FakeCalendarClient()

    status_code, body = _call("111", monkeypatch, calendar_client, event_store)

    assert body["changed"] is False
    assert calendar_client.calls == []
    assert calendar_client.delete_calls == []


def test_program_unknown_is_assumed_online(monkeypatch):
    event_store = _FakeEventStore(
        documents={
            ("Activity", "111"): {
                "program_id": 999,
                "status": "Normal",
                "location": None,
                "name": "Test",
                "end_date": "2026-10-27T13:29:00-04:00",
                "occurrences": {
                    "a": {"start": "s", "end": "e", "calendar_event_id": None},
                },
            },
        }
    )
    calendar_client = _FakeCalendarClient()

    status_code, body = _call("111", monkeypatch, calendar_client, event_store)

    assert body["visible"] is True
    assert len(calendar_client.calls) == 1
