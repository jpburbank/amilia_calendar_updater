"""
Amilia -> Google Calendar webhook receiver
Cloud Function (2nd gen), Python runtime, HTTP trigger.

Auth: a dedicated runtime service account, which the target calendar's owner
has shared the calendar with. See calendar_client.py for the setup steps and
check_access.py to verify them.

Deploy:
  gcloud services enable calendar-json.googleapis.com

  # A dedicated identity, so the calendar grant belongs to this function
  # rather than to the project-wide default compute service account.
  gcloud iam service-accounts create amilia-calendar-updater

  gcloud functions deploy amilia-calendar-updater \
    --gen2 \
    --runtime=python312 \
    --region=<REGION> \
    --source=. \
    --entry-point=amilia_webhook \
    --trigger-http \
    --no-allow-unauthenticated \
    --service-account=amilia-calendar-updater@<PROJECT_ID>.iam.gserviceaccount.com \
    --env-vars-file=env.yaml

Amilia webhook contract (see /apidocs/ApiDocs/v1webhooks.html):
  POST body: { "OrganizationId", "Context", "Action", "Name", "EventTime", "Payload": {...} }
  Must respond 200 quickly or Amilia will retry for ~72h then disable the subscription.
"""

from __future__ import annotations

import logging
import os

import functions_framework
from flask import Request, jsonify

from calendar_client import CalendarClient, is_retryable
from handlers import handle_facility_booking, handle_registration

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CALENDAR_ID = os.environ["GOOGLE_CALENDAR_ID"]

# Route by (Context) -> handler function. Each handler takes (action, payload, calendar_client).
HANDLERS = {
    "FacilityBooking": handle_facility_booking,
    "Registration": handle_registration,
}

# Every response carries one of these statuses, and each is logged at a level
# reflecting what it cost:
#
#   ok       an event reached the calendar
#   ignored  a context we deliberately don't sync (nothing was lost)
#   rejected the request never looked like an Amilia webhook
#   skipped  the handler ran but wrote nothing — a booking that is NOT on the
#            calendar and never will be without someone noticing
#   retry    transient or operator-fixable; Amilia will redeliver
#   dropped  permanent failure, booking lost, no redelivery coming
#
# Alert on `dropped`. Alert on the *rate* of `retry` and `skipped` rather than
# on single occurrences: a sustained stream of either means the calendar share
# broke or a handler path is unimplemented, but individually they are expected.
_LOG_LEVELS = {
    "ok": logging.INFO,
    "ignored": logging.INFO,
    "rejected": logging.WARNING,
    "skipped": logging.WARNING,
    "retry": logging.WARNING,
    "dropped": logging.ERROR,
}

_calendar_client: CalendarClient | None = None


def _get_calendar_client() -> CalendarClient:
    """
    One client per instance, built lazily so a cold start only pays for the
    credential fetch if a request actually arrives.

    Reusing it across invocations is safe because Cloud Functions gen2
    defaults to one concurrent request per instance. If you raise
    --concurrency, build a client per request instead: googleapiclient's
    httplib2 transport is not thread-safe.
    """
    global _calendar_client
    if _calendar_client is None:
        _calendar_client = CalendarClient(calendar_id=CALENDAR_ID)
    return _calendar_client


def _respond(status: str, http_status: int, *, result=None, exc_info=False, **fields):
    """
    Build the response and log it, together, so the two can't drift.

    Emits one `key=value` line per request, e.g.
        status=skipped context=FacilityBooking action=Update reason=no stored event mapping
    which Cloud Logging filters as textPayload:"status=skipped".
    """
    fields = {"status": status, **{k: v for k, v in fields.items() if v is not None}}
    logger.log(
        _LOG_LEVELS[status],
        " ".join(f"{k}={v}" for k, v in fields.items()),
        exc_info=exc_info,
    )

    body = dict(fields)
    if result is not None:
        body["result"] = result
    return jsonify(body), http_status


@functions_framework.http
def amilia_webhook(request: Request):
    if request.method != "POST":
        return _respond("rejected", 405, reason="method not allowed", method=request.method)

    body = request.get_json(silent=True)
    if not body:
        return _respond(
            "rejected",
            400,
            reason="missing or malformed JSON body",
            content_type=request.content_type,
        )

    context = body.get("Context")
    action = body.get("Action")
    payload = body.get("Payload", {})
    org_id = body.get("OrganizationId")

    logger.info("Received %s %s (OrganizationId=%s)", context, action, org_id)

    handler = HANDLERS.get(context)
    if handler is None:
        return _respond("ignored", 200, context=context, organization_id=org_id)

    try:
        result = handler(action=action, payload=payload, calendar_client=_get_calendar_client())
    except Exception as exc:
        return _failure_response(context, action, org_id, exc)

    if "skipped" in result:
        # The handler ran cleanly but nothing reached the calendar. With the
        # event-ID store still stubbed out this is the common path for Update
        # and Delete, and it must not read as success in the logs.
        return _respond(
            "skipped",
            200,
            context=context,
            action=action,
            organization_id=org_id,
            reason=result["skipped"],
            result=result,
        )

    return _respond(
        "ok",
        200,
        context=context,
        action=action,
        organization_id=org_id,
        calendar_event_id=result.get("calendar_event_id"),
        result=result,
    )


def _failure_response(context: str, action: str, org_id, exc: Exception):
    """
    Decide whether to spend Amilia's retry budget on this failure.

    retry (503): a redelivery could succeed once someone fixes the calendar
    share or Google recovers. The retry window doubles as the window to notice
    and fix a broken share.

    dropped (200): identical redeliveries would fail identically, and 72h of
    them costs the whole subscription. Losing one booking beats losing every
    future one, so ack and rely on the log.
    """
    status = "retry" if is_retryable(exc) else "dropped"
    return _respond(
        status,
        503 if status == "retry" else 200,
        exc_info=True,
        context=context,
        action=action,
        organization_id=org_id,
        error=type(exc).__name__,
        google_status=getattr(getattr(exc, "resp", None), "status", None),
    )
