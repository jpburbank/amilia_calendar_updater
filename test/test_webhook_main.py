"""Every response type the webhook can emit, and the level it logs at."""

import json
import logging
import os
from datetime import UTC, datetime, timedelta

import httplib2
import pytest
from flask import Flask, request
from googleapiclient.errors import HttpError

os.environ.setdefault("GOOGLE_CALENDAR_ID", "test-calendar@group.calendar.google.com")
os.environ.setdefault("GOOGLE_EVENT_STORE_BUCKET", "test-bucket")
os.environ.setdefault("AMILIA_WEBHOOK_TOKEN", "test-token")
os.environ.setdefault("AMILIA_API_USERNAME", "test-user")
os.environ.setdefault("AMILIA_API_PASSWORD", "test-password")
os.environ.setdefault("GCP_PROJECT_ID", "test-project")
os.environ.setdefault("RECONCILE_QUEUE_ID", "test-queue")
os.environ.setdefault("RECONCILE_WORKER_URL", "https://example.invalid/reconcile")
os.environ.setdefault("RECONCILE_QUEUE_LOCATION", "test-region")
os.environ.setdefault(
    "RECONCILE_INVOKER_SERVICE_ACCOUNT", "test-sa@test-project.iam.gserviceaccount.com"
)
os.environ.setdefault("WEBHOOK_QUEUE_ID", "test-webhook-queue")
os.environ.setdefault("WEBHOOK_WORKER_URL", "https://example.invalid/worker")
os.environ.setdefault("WEBHOOK_TASK_GIVE_UP_AFTER_SECONDS", "86400")

# src/webhook/main.py and src/reconciler/main.py are both literally named
# main.py (a Cloud Functions constraint — see either module's docstring),
# so a plain `import main` would collide between the two test files in
# this same pytest session (and silently return whichever loaded first,
# via sys.modules). Loading by explicit file path with a distinct name
# avoids that, while still exercising main.py's own imports exactly as
# Cloud Functions does — via whatever's on sys.path (src, src/webhook;
# see pyproject.toml's pythonpath).
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "webhook_main", Path(__file__).parent.parent / "src" / "webhook" / "main.py"
)
webhook_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(webhook_main)  # noqa: E402  (import needs the env vars above set)

# Captured before the autouse fixture below stubs it out, so the queue's
# configuration wiring can still be tested directly.
_real_get_task_queue = webhook_main._get_task_queue
_real_get_webhook_queue = webhook_main._get_webhook_queue


class _FakeWebhookQueue:
    def __init__(self, error=None):
        self.enqueued = []
        self.error = error

    def enqueue_webhook(self, webhook):
        if self.error:
            raise self.error
        self.enqueued.append(webhook)


app = Flask(__name__)


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch):
    """Keep the tests off the metadata server and any real network calls."""
    monkeypatch.setattr(webhook_main, "_get_calendar_client", lambda: object())
    monkeypatch.setattr(webhook_main, "_get_event_store", lambda: object())
    monkeypatch.setattr(webhook_main, "_get_amilia_client", lambda: object())
    monkeypatch.setattr(webhook_main, "_get_task_queue", lambda: object())
    monkeypatch.setattr(webhook_main, "_get_webhook_queue", lambda: _FakeWebhookQueue())


def call(method="POST", json_body=None, handler=None, monkeypatch=None, token="test-token"):
    """
    Invoke the webhook, returning (status_code, body).

    Defaults to a valid token so every test exercises the behavior it's
    named for, not the token check — token=None omits it entirely, and any
    other value tests the check itself.
    """
    if handler is not None:
        monkeypatch.setitem(webhook_main.HANDLERS, "FacilityBooking", handler)
    query_string = {"token": token} if token is not None else {}
    with app.test_request_context(method=method, json=json_body, query_string=query_string):
        response, status_code = webhook_main.amilia_webhook(request)
        return status_code, response.get_json()


def booking(action="Create"):
    return {"OrganizationId": 42, "Context": "FacilityBooking", "Action": action, "Payload": {}}


def call_worker(webhook, handler=None, monkeypatch=None, enqueued_at=None, retry_count="0"):
    """Invoke the worker entry point as Cloud Tasks would, returning (status_code, body)."""
    if handler is not None:
        monkeypatch.setitem(webhook_main.HANDLERS, "FacilityBooking", handler)
    task = {
        "webhook": webhook,
        "enqueued_at": (enqueued_at or datetime.now(UTC)).isoformat(),
    }
    headers = {"X-CloudTasks-TaskRetryCount": retry_count}
    with app.test_request_context(method="POST", json=task, headers=headers):
        response, status_code = webhook_main.process_webhook_task(request)
        return status_code, response.get_json()


def inline(monkeypatch, handler):
    """Make enqueueing fail, so the webhook processes inline with `handler`."""
    monkeypatch.setitem(webhook_main.HANDLERS, "FacilityBooking", handler)
    monkeypatch.setattr(
        webhook_main, "_get_webhook_queue", lambda: _FakeWebhookQueue(error=RuntimeError("down"))
    )


def test_missing_token_is_rejected(caplog):
    with caplog.at_level(logging.DEBUG):
        status_code, body = call(json_body=booking(), token=None)
    assert status_code == 403
    assert body["status"] == "rejected"
    assert caplog.records[-1].levelno == logging.WARNING


def test_wrong_token_is_rejected(caplog):
    with caplog.at_level(logging.DEBUG):
        status_code, body = call(json_body=booking(), token="not-the-right-token")
    assert status_code == 403
    assert body["status"] == "rejected"


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


def test_facility_booking_context_is_ignored_by_default(caplog):
    """FACILITY_BOOKING_SYNC_ENABLED is unset in tests, so the flag's default (off) applies."""
    with caplog.at_level(logging.DEBUG):
        status_code, body = call(json_body=booking())
    assert status_code == 200
    assert body["status"] == "ignored"
    assert body["context"] == "FacilityBooking"


def test_facility_booking_handler_is_routed_only_when_enabled():
    assert "FacilityBooking" in webhook_main._build_handlers(facility_booking_enabled=True)
    assert "FacilityBooking" not in webhook_main._build_handlers(facility_booking_enabled=False)


def test_program_and_activity_are_routed_regardless_of_facility_booking_flag():
    for enabled in (True, False):
        assert {"Program", "Activity"} <= webhook_main._build_handlers(enabled).keys()


def test_written_event_is_ok(caplog, monkeypatch):
    def handler(**kw):
        return {"reservation_id": 7, "calendar_event_id": "evt_abc"}

    with caplog.at_level(logging.DEBUG):
        status_code, body = call_worker(booking(), handler=handler, monkeypatch=monkeypatch)
    assert status_code == 200
    assert body["status"] == "ok"
    assert body["result"]["calendar_event_id"] == "evt_abc"
    assert caplog.records[-1].levelno == logging.INFO
    assert "calendar_event_id=evt_abc" in caplog.records[-1].getMessage()


def test_event_time_is_passed_to_the_handler(monkeypatch):
    received = {}

    def handler(**kw):
        received.update(kw)
        return {"reservation_id": 7, "calendar_event_id": "evt_abc"}

    body = {**booking(), "EventTime": "2026-11-13T12:35:01.4348239-05:00"}
    call_worker(body, handler=handler, monkeypatch=monkeypatch)

    assert received["event_time"] == "2026-11-13T12:35:01.4348239-05:00"


def test_handler_writing_nothing_is_skipped_not_ok(caplog, monkeypatch):
    """A no-op booking must not read as success."""

    def handler(**kw):
        return {"reservation_id": 7, "skipped": "no stored event mapping"}

    with caplog.at_level(logging.DEBUG):
        status_code, body = call_worker(booking("Update"), handler=handler, monkeypatch=monkeypatch)
    assert status_code == 200
    assert body["status"] == "skipped"
    assert caplog.records[-1].levelno == logging.WARNING
    assert "reason=no stored event mapping" in caplog.records[-1].getMessage()


def test_unshared_calendar_is_dropped_at_error_level_when_inline(caplog, monkeypatch):
    """
    Processing inline (enqueue failed), even an operator-fixable failure
    (calendar not shared yet) is always acked with 200 rather than left
    for Amilia to retry.
    """

    def handler(**kw):
        raise HttpError(httplib2.Response({"status": 403}), b"{}")

    inline(monkeypatch, handler)
    with caplog.at_level(logging.DEBUG):
        status_code, body = call(json_body=booking())
    assert status_code == 200
    assert body["status"] == "dropped"
    assert caplog.records[-1].levelno == logging.ERROR
    assert "google_status=403" in caplog.records[-1].getMessage()


def test_malformed_payload_is_dropped_at_error_level_when_inline(caplog, monkeypatch):
    def handler(**kw):
        raise KeyError("Start")

    inline(monkeypatch, handler)
    with caplog.at_level(logging.DEBUG):
        status_code, body = call(json_body=booking())
    assert status_code == 200
    assert body["status"] == "dropped"
    assert caplog.records[-1].levelno == logging.ERROR
    assert caplog.records[-1].exc_info is not None


def test_every_status_has_a_log_level():
    """Guards against a new _respond() call site with no level mapped."""
    assert set(webhook_main._LOG_LEVELS) == {
        "ok",
        "queued",
        "ignored",
        "rejected",
        "skipped",
        "retrying",
        "dropped",
        "discarded",
    }


def test_task_queue_is_configured_from_environment(monkeypatch):
    """Region and invoker identity come from env.yaml, not values baked into the code."""
    captured = {}
    monkeypatch.setattr(webhook_main, "ReconcileQueue", lambda **kw: captured.update(kw) or kw)
    monkeypatch.setattr(webhook_main, "_task_queue", None)
    _real_get_task_queue()
    assert captured == {
        "project_id": "test-project",
        "location": "test-region",
        "queue_id": "test-queue",
        "worker_url": "https://example.invalid/reconcile",
        "invoker_service_account": "test-sa@test-project.iam.gserviceaccount.com",
    }


def test_webhook_queue_is_configured_from_environment(monkeypatch):
    captured = {}
    monkeypatch.setattr(webhook_main, "WebhookQueue", lambda **kw: captured.update(kw) or kw)
    monkeypatch.setattr(webhook_main, "_webhook_queue", None)
    _real_get_webhook_queue()
    assert captured == {
        "project_id": "test-project",
        "location": "test-region",
        "queue_id": "test-webhook-queue",
        "worker_url": "https://example.invalid/worker",
        "invoker_service_account": "test-sa@test-project.iam.gserviceaccount.com",
    }


# ---------------------------------------------------------------------------
# Public webhook: enqueue, don't process
# ---------------------------------------------------------------------------


def _activity_webhook():
    return {
        "OrganizationId": 17659,
        "Context": "Activity",
        "Action": "Update",
        "EventTime": "2026-11-13T12:40:28.1983443-05:00",
        "Payload": {"Id": 7482865, "Name": "Test - Bandsaw Bear Claws"},
    }


def test_routable_webhook_is_queued_not_processed(caplog, monkeypatch):
    queue = _FakeWebhookQueue()
    monkeypatch.setattr(webhook_main, "_get_webhook_queue", lambda: queue)

    def handler(**kw):
        raise AssertionError("the public webhook must not run handlers")

    monkeypatch.setitem(webhook_main.HANDLERS, "Activity", handler)
    with caplog.at_level(logging.DEBUG):
        status_code, body = call(json_body=_activity_webhook())

    assert status_code == 200
    assert body["status"] == "queued"
    assert queue.enqueued == [_activity_webhook()]
    assert caplog.records[-1].levelno == logging.INFO


def test_unroutable_webhook_is_ignored_without_enqueueing(monkeypatch):
    queue = _FakeWebhookQueue()
    monkeypatch.setattr(webhook_main, "_get_webhook_queue", lambda: queue)

    status_code, body = call(json_body={"Context": "Registration", "Action": "Create"})

    assert body["status"] == "ignored"
    assert queue.enqueued == []


def test_enqueue_failure_processes_inline_and_fails_open(caplog, monkeypatch):
    received = {}

    def handler(**kw):
        received.update(kw)
        return {"reservation_id": 7, "calendar_event_id": "evt_abc"}

    inline(monkeypatch, handler)
    with caplog.at_level(logging.DEBUG):
        status_code, body = call(json_body=booking())

    assert status_code == 200
    assert body["status"] == "ok"
    assert received["fail_open"] is True
    assert "could not enqueue webhook; processing inline" in caplog.text


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _transient_error():
    return HttpError(httplib2.Response({"status": 503}), b"{}")


def test_worker_does_not_fail_open_while_retries_remain(monkeypatch):
    received = {}

    def handler(**kw):
        received.update(kw)
        return {"reservation_id": 7, "calendar_event_id": "evt_abc"}

    call_worker(booking(), handler=handler, monkeypatch=monkeypatch)

    assert received["fail_open"] is False


def test_worker_transient_failure_answers_503_so_cloud_tasks_retries(caplog, monkeypatch):
    def handler(**kw):
        raise _transient_error()

    with caplog.at_level(logging.DEBUG):
        status_code, body = call_worker(
            booking(), handler=handler, monkeypatch=monkeypatch, retry_count="3"
        )

    assert status_code == 503
    assert body["status"] == "retrying"
    assert body["attempt"] == "3"
    assert caplog.records[-1].levelno == logging.WARNING


def test_worker_permanent_failure_is_discarded_with_the_full_webhook(caplog, monkeypatch):
    def handler(**kw):
        raise KeyError("Start")

    webhook = {**booking(), "Payload": {"ReservationId": "FB-1", "Title": "Laser"}}
    with caplog.at_level(logging.DEBUG):
        status_code, body = call_worker(webhook, handler=handler, monkeypatch=monkeypatch)

    assert status_code == 200
    assert body["status"] == "discarded"
    assert body["reason"] == "permanent failure"
    assert json.loads(body["webhook"]) == webhook
    record = caplog.records[-1]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert json.loads(record.getMessage().split(" webhook=", 1)[1]) == webhook


def test_worker_final_attempt_fails_open(monkeypatch):
    received = {}

    def handler(**kw):
        received.update(kw)
        return {"reservation_id": 7, "calendar_event_id": "evt_abc"}

    long_ago = datetime.now(UTC) - timedelta(hours=24, minutes=1)
    status_code, body = call_worker(
        booking(), handler=handler, monkeypatch=monkeypatch, enqueued_at=long_ago
    )

    assert received["fail_open"] is True
    assert body["status"] == "ok"


def test_worker_final_attempt_failure_is_discarded_even_if_transient(caplog, monkeypatch):
    def handler(**kw):
        raise _transient_error()

    long_ago = datetime.now(UTC) - timedelta(hours=24, minutes=1)
    with caplog.at_level(logging.DEBUG):
        status_code, body = call_worker(
            booking(), handler=handler, monkeypatch=monkeypatch, enqueued_at=long_ago
        )

    assert status_code == 200
    assert body["status"] == "discarded"
    assert body["reason"] == "retry window exhausted"
    assert caplog.records[-1].levelno == logging.ERROR


def test_worker_malformed_task_is_discarded(caplog):
    with caplog.at_level(logging.DEBUG):
        with app.test_request_context(method="POST", json={"not": "a task"}):
            response, status_code = webhook_main.process_webhook_task(request)

    assert status_code == 200
    assert response.get_json()["status"] == "discarded"
    assert '{"not": "a task"}' in caplog.records[-1].getMessage()


def test_worker_ignores_contexts_no_longer_routed():
    status_code, body = call_worker({"Context": "Registration", "Action": "Create"})

    assert status_code == 200
    assert body["status"] == "ignored"


@pytest.mark.parametrize(
    "enqueued_at, expected",
    [
        (timedelta(hours=23, minutes=59), False),
        (timedelta(hours=24), True),
        (None, True),
    ],
)
def test_is_final_attempt(enqueued_at, expected):
    now = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    value = (now - enqueued_at).isoformat() if enqueued_at is not None else None

    assert webhook_main._is_final_attempt(value, now=now) is expected


def test_is_final_attempt_treats_unreadable_time_as_final():
    assert webhook_main._is_final_attempt("not-a-time") is True
