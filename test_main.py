"""Every response type the webhook can emit, and the level it logs at."""

import logging
import os

import httplib2
import pytest
from flask import Flask, request
from googleapiclient.errors import HttpError

os.environ.setdefault("GOOGLE_CALENDAR_ID", "test-calendar@group.calendar.google.com")
os.environ.setdefault("GOOGLE_EVENT_STORE_BUCKET", "test-bucket")

import main  # noqa: E402  (import needs GOOGLE_CALENDAR_ID/GOOGLE_EVENT_STORE_BUCKET set)

app = Flask(__name__)


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch):
    """Keep the tests off the metadata server."""
    monkeypatch.setattr(main, "_get_calendar_client", lambda: object())
    monkeypatch.setattr(main, "_get_event_store", lambda: object())


def call(method="POST", json_body=None, handler=None, monkeypatch=None):
    """Invoke the webhook, returning (status_code, body, handler_arg_seen)."""
    if handler is not None:
        monkeypatch.setitem(main.HANDLERS, "FacilityBooking", handler)
    with app.test_request_context(method=method, json=json_body):
        response, status_code = main.amilia_webhook(request)
        return status_code, response.get_json()


def booking(action="Create"):
    return {"OrganizationId": 42, "Context": "FacilityBooking", "Action": action, "Payload": {}}


def test_non_post_is_rejected(caplog):
    with caplog.at_level(logging.DEBUG):
        status_code, body = call(method="GET")
    assert status_code == 405
    assert body["status"] == "rejected"
    assert caplog.records[-1].levelno == logging.WARNING


def test_unparseable_body_is_rejected(caplog):
    with caplog.at_level(logging.DEBUG):
        status_code, body = call(json_body=None)
    assert status_code == 400
    assert body["status"] == "rejected"
    assert caplog.records[-1].levelno == logging.WARNING


def test_unknown_context_is_ignored(caplog):
    with caplog.at_level(logging.DEBUG):
        status_code, body = call(json_body={"Context": "Membership", "Action": "Create"})
    assert status_code == 200
    assert body["status"] == "ignored"
    assert caplog.records[-1].levelno == logging.INFO


def test_registration_context_is_ignored(caplog):
    """Registration webhooks are deliberately not synced (for now)."""
    with caplog.at_level(logging.DEBUG):
        status_code, body = call(json_body={"Context": "Registration", "Action": "Create"})
    assert status_code == 200
    assert body["status"] == "ignored"
    assert caplog.records[-1].levelno == logging.INFO


def test_written_event_is_ok(caplog, monkeypatch):
    handler = lambda **kw: {"reservation_id": 7, "calendar_event_id": "evt_abc"}
    with caplog.at_level(logging.DEBUG):
        status_code, body = call(json_body=booking(), handler=handler, monkeypatch=monkeypatch)
    assert status_code == 200
    assert body["status"] == "ok"
    assert body["result"]["calendar_event_id"] == "evt_abc"
    assert caplog.records[-1].levelno == logging.INFO
    assert "calendar_event_id=evt_abc" in caplog.records[-1].getMessage()


def test_handler_writing_nothing_is_skipped_not_ok(caplog, monkeypatch):
    """A no-op booking must not read as success."""
    handler = lambda **kw: {"reservation_id": 7, "skipped": "no stored event mapping"}
    with caplog.at_level(logging.DEBUG):
        status_code, body = call(
            json_body=booking("Update"), handler=handler, monkeypatch=monkeypatch
        )
    assert status_code == 200
    assert body["status"] == "skipped"
    assert caplog.records[-1].levelno == logging.WARNING
    assert "reason=no stored event mapping" in caplog.records[-1].getMessage()


def test_unshared_calendar_is_dropped_at_error_level(caplog, monkeypatch):
    """
    Even an operator-fixable failure (calendar not shared yet) is always
    acked with 200 rather than left for Amilia to retry.
    """

    def handler(**kw):
        raise HttpError(httplib2.Response({"status": 403}), b"{}")

    with caplog.at_level(logging.DEBUG):
        status_code, body = call(json_body=booking(), handler=handler, monkeypatch=monkeypatch)
    assert status_code == 200
    assert body["status"] == "dropped"
    assert caplog.records[-1].levelno == logging.ERROR
    assert "google_status=403" in caplog.records[-1].getMessage()


def test_malformed_payload_is_dropped_at_error_level(caplog, monkeypatch):
    def handler(**kw):
        raise KeyError("Start")

    with caplog.at_level(logging.DEBUG):
        status_code, body = call(json_body=booking(), handler=handler, monkeypatch=monkeypatch)
    assert status_code == 200
    assert body["status"] == "dropped"
    assert caplog.records[-1].levelno == logging.ERROR
    assert caplog.records[-1].exc_info is not None


def test_every_status_has_a_log_level():
    """Guards against a new _respond() call site with no level mapped."""
    assert set(main._LOG_LEVELS) == {"ok", "ignored", "rejected", "skipped", "dropped"}
