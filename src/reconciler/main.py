"""
Cloud Tasks worker: reconciles one Activity's calendar presence against its
current stored state (Program.online AND Activity.status == "Normal").
Deployed as its own Cloud Function (amilia-activity-reconciler),
--no-allow-unauthenticated, invoked only by Cloud Tasks via a signed OIDC
token — never reachable from the public internet the way amilia_webhook is.

Unlike amilia_webhook, this does NOT always ack 200 regardless of outcome.
Amilia's own retry mechanism is costly (72h then it disables the whole
subscription), which is why amilia_webhook always acks — but Cloud Tasks'
retry mechanism is ours to configure and is actually useful here, so a
real failure returns a real error status and lets Cloud Tasks retry that
one task on its own schedule instead.

Does not call the Amilia REST API. Each occurrence's Start/End is already
stored on the Activity's own document from its last Create/Update (see
../webhook/handlers.py); only whether it should currently be *on* the
calendar changes here, never when or where it happens.

Layout: this directory (src/reconciler/) is exactly what --source points
at, deliberately separate from src/webhook/ (Cloud Functions' buildpack
deploy always imports a file named main.py from --source, so two
functions can't share one source tree even though they share code) —
this one intentionally carries none of the webhook-only dependencies
(Cloud Tasks client, requests, handlers.py, amilia_client.py). shared/
here is a generated copy of ../shared/, not hand-maintained — run
scripts/build.sh from the repo root before every deploy to refresh it.

Deploy (from the repo root):
  ./scripts/build.sh

  gcloud functions deploy amilia-activity-reconciler \
    --gen2 \
    --runtime=python312 \
    --region=<REGION> \
    --source=src/reconciler \
    --entry-point=reconcile_activity \
    --trigger-http \
    --no-allow-unauthenticated \
    --service-account=amilia-calendar-updater@<PROJECT_ID>.iam.gserviceaccount.com \
    --env-vars-file=env.yaml
"""

from __future__ import annotations

import logging
import os

import functions_framework
from flask import Request, jsonify

from shared.calendar_client import CalendarClient
from shared.event_store import EventStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CALENDAR_ID = os.environ["GOOGLE_CALENDAR_ID"]
EVENT_STORE_BUCKET = os.environ["GOOGLE_EVENT_STORE_BUCKET"]

_calendar_client: CalendarClient | None = None
_event_store: EventStore | None = None


def _get_calendar_client() -> CalendarClient:
    global _calendar_client
    if _calendar_client is None:
        _calendar_client = CalendarClient(calendar_id=CALENDAR_ID)
    return _calendar_client


def _get_event_store() -> EventStore:
    global _event_store
    if _event_store is None:
        _event_store = EventStore(bucket_name=EVENT_STORE_BUCKET)
    return _event_store


@functions_framework.http
def reconcile_activity(request: Request):
    body = request.get_json(silent=True) or {}
    activity_id = body.get("activity_id")
    if not activity_id:
        return jsonify({"error": "missing activity_id"}), 400

    calendar_client = _get_calendar_client()
    event_store = _get_event_store()

    activity = event_store.get("Activity", activity_id)
    if activity is None:
        # The activity was deleted after this task was enqueued but before
        # it ran — nothing to reconcile, and not an error worth retrying.
        logger.info("activity_id=%s skipped=no stored activity", activity_id)
        return jsonify({"activity_id": activity_id, "skipped": "no stored activity"}), 200

    program_id = activity.get("program_id")
    program = event_store.get("Program", program_id) if program_id else None
    program_online = True if program is None else program.get("online", True)
    visible = program_online and activity.get("status") == "Normal"

    occurrences = activity.get("occurrences") or {}
    changed = False
    for occurrence in occurrences.values():
        calendar_event_id = occurrence.get("calendar_event_id")
        if visible and not calendar_event_id:
            event = calendar_client.create_event(
                summary=activity.get("name", "Activity"),
                start_iso=occurrence["start"],
                end_iso=occurrence["end"],
                location=activity.get("location"),
            )
            occurrence["calendar_event_id"] = event["id"]
            changed = True
        elif not visible and calendar_event_id:
            calendar_client.delete_event(calendar_event_id)
            occurrence["calendar_event_id"] = None
            changed = True

    if changed:
        event_store.set(
            "Activity",
            activity_id,
            {**activity, "occurrences": occurrences},
            custom_time=activity.get("end_date"),
        )

    logger.info("activity_id=%s visible=%s changed=%s", activity_id, visible, changed)
    return jsonify({"activity_id": activity_id, "visible": visible, "changed": changed}), 200
