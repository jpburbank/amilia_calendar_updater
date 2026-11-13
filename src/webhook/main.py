"""
Amilia -> Google Calendar webhook receiver
Cloud Function (2nd gen), Python runtime, HTTP trigger.

Auth: a dedicated runtime service account, which the target calendar's owner
has shared the calendar with. See shared/calendar_client.py for the setup
steps and scripts/check_access.py to verify them.

Layout: this directory (src/webhook/) is exactly what --source points at —
Cloud Functions' buildpack deploy always imports a file named main.py from
the --source directory, so this function and reconciler (src/reconciler/)
can't share one source tree despite sharing code. shared/ here is a
generated copy of ../shared/, not hand-maintained — run
scripts/build.sh from the repo root before every deploy to refresh it.

Deploy:
  gcloud services enable calendar-json.googleapis.com

  # The runtime service account (a dedicated identity, so the calendar grant
  # belongs to this function rather than the project-wide default compute
  # service account) and the event-ID mapping bucket, with its IAM binding
  # and retention lifecycle, are Terraform-managed in the sibling
  # amilia_calendar_updater_gcp_resources project — run `terraform apply`
  # there first.

  # Amilia has no webhook-signing mechanism, so requests are authenticated
  # by a shared-secret token instead (see the check at the top of
  # amilia_webhook). The token lives in Secret Manager, provisioned by the
  # sibling amilia_calendar_updater_gcp_resources Terraform project — run
  # `terraform apply` there first, then mount it as an env var at deploy:
  #   --set-secrets="AMILIA_WEBHOOK_TOKEN=amilia-webhook-token:latest"
  # Register the webhook with Amilia at this function's URL with the token
  # appended as a query param, e.g. "?token=<value>" — fetch the value with:
  #   gcloud secrets versions access latest --secret=amilia-webhook-token

  # Program/Activity sync needs three more things, all set up by the same
  # Terraform project — see amilia_client.py, reconcile_queue.py, and
  # ../reconciler/main.py:
  #   1. A dedicated Amilia user for REST API access (occurrence data the
  #      webhooks don't carry). Terraform creates the amilia-api-username /
  #      amilia-api-password secret *containers* only — set the real values
  #      manually (see secrets.tf's comment) before deploying.
  #   2. The Cloud Tasks queue used to fan out Program visibility flips.
  #   3. ../reconciler/main.py deployed as its own Cloud Function
  #      (amilia-activity-reconciler, --no-allow-unauthenticated — see its
  #      own module docstring for that deploy command) *before* this
  #      function's first deploy, since RECONCILE_WORKER_URL below needs
  #      its URL.

  # The target calendar ID is the google-calendar-id secret (also from the
  # Terraform project; the value is set by hand), not an env.yaml entry, so
  # switching dev/prod calendars is a new secret version plus a redeploy.
  # See README.md "Target calendar".

  # FacilityBooking sync is off unless env.yaml sets
  # FACILITY_BOOKING_SYNC_ENABLED: "true" — see shared/feature_flags.py.

  # From the repo root:
  ./scripts/build.sh

  gcloud functions deploy amilia-calendar-updater \
    --gen2 \
    --runtime=python312 \
    --region=<REGION> \
    --source=src/webhook \
    --entry-point=amilia_webhook \
    --trigger-http \
    --allow-unauthenticated \
    --service-account=<SA_EMAIL> \
    --env-vars-file=env.yaml \
    --set-secrets="GOOGLE_CALENDAR_ID=google-calendar-id:latest,AMILIA_WEBHOOK_TOKEN=amilia-webhook-token:latest,AMILIA_API_USERNAME=amilia-api-username:latest,AMILIA_API_PASSWORD=amilia-api-password:latest"

Amilia webhook contract (see /apidocs/ApiDocs/v1webhooks.html):
  POST body: { "OrganizationId", "Context", "Action", "Name", "EventTime", "Payload": {...} }
  Must respond 200 quickly or Amilia will retry for ~72h then disable the subscription.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sys

import functions_framework
from flask import Request, jsonify

from amilia_client import AmiliaClient
from handlers import handle_activity, handle_facility_booking, handle_program
from reconcile_queue import ReconcileQueue
from shared.calendar_client import CalendarClient
from shared.event_store import EventStore
from shared.feature_flags import facility_booking_sync_enabled


class _StructuredFormatter(logging.Formatter):
    """
    One JSON object per line, per Cloud Logging's structured-logging
    convention: https://cloud.google.com/logging/docs/structured-logging

    Plain text through logging.basicConfig() always lands in Cloud Logging
    as severity=DEFAULT. Writing a top-level "severity" field instead gets
    it parsed into the LogEntry's actual severity, which is what the
    "Alert on `dropped`" logging strategy below depends on.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {"severity": record.levelname, "message": record.getMessage()}
        if record.exc_info:
            payload["message"] += "\n" + self.formatException(record.exc_info)
        return json.dumps(payload)


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_StructuredFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


_configure_logging()
logger = logging.getLogger(__name__)

CALENDAR_ID = os.environ["GOOGLE_CALENDAR_ID"]
EVENT_STORE_BUCKET = os.environ["GOOGLE_EVENT_STORE_BUCKET"]
WEBHOOK_TOKEN = os.environ["AMILIA_WEBHOOK_TOKEN"]
AMILIA_API_USERNAME = os.environ["AMILIA_API_USERNAME"]
AMILIA_API_PASSWORD = os.environ["AMILIA_API_PASSWORD"]
GCP_PROJECT_ID = os.environ["GCP_PROJECT_ID"]
RECONCILE_QUEUE_ID = os.environ["RECONCILE_QUEUE_ID"]
RECONCILE_WORKER_URL = os.environ["RECONCILE_WORKER_URL"]
# The Cloud Tasks queue's region, and the identity Cloud Tasks signs its
# OIDC token as when calling the reconciler (this function's own runtime
# service account; Terraform's amilia_calendar_updater_email output).
RECONCILE_QUEUE_LOCATION = os.environ["RECONCILE_QUEUE_LOCATION"]
RECONCILE_INVOKER_SERVICE_ACCOUNT = os.environ["RECONCILE_INVOKER_SERVICE_ACCOUNT"]


def _build_handlers(facility_booking_enabled: bool) -> dict:
    """
    Route by (Context) -> handler function. Each handler is called with the
    same full set of keyword arguments (action, payload, calendar_client,
    event_store, org_id, amilia_client, task_queue) and absorbs whichever it
    doesn't need via **_ignored. Contexts not listed here (e.g. Registration,
    or FacilityBooking while FACILITY_BOOKING_SYNC_ENABLED is off — see
    shared/feature_flags.py) fall through to the "ignored" 200 response below.
    """
    handlers = {
        "Program": handle_program,
        "Activity": handle_activity,
    }
    if facility_booking_enabled:
        handlers["FacilityBooking"] = handle_facility_booking
    return handlers


HANDLERS = _build_handlers(facility_booking_sync_enabled())

# Every response carries one of these statuses, and each is logged at a level
# reflecting what it cost:
#
#   ok       an event reached the calendar
#   ignored  a context we deliberately don't sync (nothing was lost)
#   rejected the request never looked like an Amilia webhook
#   skipped  the handler ran but wrote nothing — a booking that is NOT on the
#            calendar and never will be without someone noticing
#   dropped  the handler raised — booking lost, no redelivery: we always ack
#            with 200 so a bad booking or an outage never burns Amilia's ~72h
#            retry budget or gets the whole subscription disabled
#
# Alert on `dropped`. Alert on the *rate* of `skipped` rather than on single
# occurrences: a sustained stream means a handler path is unimplemented, but
# individually it's expected.
_LOG_LEVELS = {
    "ok": logging.INFO,
    "ignored": logging.INFO,
    "rejected": logging.WARNING,
    "skipped": logging.WARNING,
    "dropped": logging.ERROR,
}

_calendar_client: CalendarClient | None = None
_event_store: EventStore | None = None
_amilia_client: AmiliaClient | None = None
_task_queue: ReconcileQueue | None = None


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


def _get_event_store() -> EventStore:
    """One store per instance, built lazily — see _get_calendar_client()."""
    global _event_store
    if _event_store is None:
        _event_store = EventStore(bucket_name=EVENT_STORE_BUCKET)
    return _event_store


def _get_amilia_client() -> AmiliaClient:
    """One client per instance, built lazily — see _get_calendar_client()."""
    global _amilia_client
    if _amilia_client is None:
        _amilia_client = AmiliaClient(username=AMILIA_API_USERNAME, password=AMILIA_API_PASSWORD)
    return _amilia_client


def _get_task_queue() -> ReconcileQueue:
    """One client per instance, built lazily — see _get_calendar_client()."""
    global _task_queue
    if _task_queue is None:
        _task_queue = ReconcileQueue(
            project_id=GCP_PROJECT_ID,
            location=RECONCILE_QUEUE_LOCATION,
            queue_id=RECONCILE_QUEUE_ID,
            worker_url=RECONCILE_WORKER_URL,
            invoker_service_account=RECONCILE_INVOKER_SERVICE_ACCOUNT,
        )
    return _task_queue


def _respond(status: str, http_status: int, *, result=None, exc_info=False, **fields):
    """
    Build the response and log it, together, so the two can't drift.

    Emits one `key=value` line per request, e.g.
        status=skipped context=FacilityBooking action=Update reason=no stored event mapping
    which Cloud Logging filters as jsonPayload.message:"status=skipped".
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
    # Amilia has no webhook-signing mechanism, so the shared secret travels
    # as a query param on the URL registered with Amilia rather than a
    # header. Checked first, before any other work, so a forged request
    # never reaches JSON parsing or a handler. compare_digest avoids a
    # timing side-channel that could let an attacker infer the token.
    if not hmac.compare_digest(request.args.get("token", ""), WEBHOOK_TOKEN):
        return _respond("rejected", 403, reason="invalid or missing token")

    # TEMP DEBUG — remove before merging. First thing after the auth check:
    # confirms the function was invoked (with a valid token) at all,
    # independent of method/body validity or anything downstream. Filter
    # with: textPayload:"AMILIA_WEBHOOK_INVOKED"
    logger.info("AMILIA_WEBHOOK_INVOKED method=%s", request.method)

    if request.method != "POST":
        return _respond("rejected", 405, reason="method not allowed", method=request.method)

    # TEMP DEBUG — remove before merging. Logs the complete, raw request Amilia
    # sends so we can see the real payload shape in GCP Logs Explorer. Filter
    # with: textPayload:"AMILIA_RAW_PAYLOAD"
    logger.info(
        "AMILIA_RAW_PAYLOAD headers=%s body=%s",
        dict(request.headers),
        request.get_data(as_text=True),
    )

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
        result = handler(
            action=action,
            payload=payload,
            event_time=body.get("EventTime"),
            calendar_client=_get_calendar_client(),
            event_store=_get_event_store(),
            org_id=org_id,
            amilia_client=_get_amilia_client(),
            task_queue=_get_task_queue(),
        )
    except Exception as exc:
        return _failure_response(context, action, org_id, exc)

    if "skipped" in result:
        # The handler ran cleanly but nothing reached the calendar — e.g. an
        # Update/Delete for a booking with no stored event mapping. Must not
        # read as success in the logs.
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
    Always ack with 200: a redelivery of the identical request would fail
    identically, and 72h of retries costs the whole Amilia subscription.
    Losing one booking beats losing every future one, so ack and rely on the
    error log (and alerting on it) to catch and fix the underlying problem.
    """
    return _respond(
        "dropped",
        200,
        exc_info=True,
        context=context,
        action=action,
        organization_id=org_id,
        error=type(exc).__name__,
        google_status=getattr(getattr(exc, "resp", None), "status", None),
    )
