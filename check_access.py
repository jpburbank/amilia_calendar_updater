"""
Diagnostic: confirms the current credentials can actually write events to
GOOGLE_CALENDAR_ID. Run it after sharing the calendar, before pointing
Amilia at the deployed function.

Not imported by the function — this is a manual, run-it-yourself script.

As the function's own identity (the check that matters):
    gcloud auth application-default login \
      --impersonate-service-account=<SA_EMAIL>
    GOOGLE_CALENDAR_ID=... python check_access.py

Impersonating needs roles/iam.serviceAccountTokenCreator on that service
account. Running it as yourself instead only tells you the calendar ID is
valid — your own access says nothing about the service account's.

Creates and immediately deletes a probe event dated a year in the past.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import google.auth
import google.auth.transport.requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from calendar_client import SCOPES

PROBE_SUMMARY = "amilia-calendar-sync access check (safe to delete)"


def _identity(credentials) -> str:
    """Best-effort email for whoever we authenticated as."""
    email = getattr(credentials, "service_account_email", None)
    if not email or email == "default":
        # Compute/Cloud Run credentials only learn their own email on refresh.
        credentials.refresh(google.auth.transport.requests.Request())
        email = getattr(credentials, "service_account_email", None)
    return email or "<not a service account — probably your user account>"


def _explain(exc: HttpError, calendar_id: str) -> str:
    status = exc.resp.status
    if status == 404:
        return (
            f"404: calendar '{calendar_id}' is not visible to this identity — either the ID is "
            "wrong or the calendar has not been shared at all. The ID is in Calendar settings "
            "under 'Integrate calendar' -> 'Calendar ID'."
        )
    if status == 403:
        return (
            "403: the calendar is visible but not writable. Re-share it with 'Make changes to "
            "events' rather than 'See all event details'. If you just changed the sharing, give "
            "it a minute. If sharing looks right, check the API is enabled: "
            "gcloud services enable calendar-json.googleapis.com"
        )
    return f"{status}: {exc}"


def main() -> int:
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    if not calendar_id:
        print("GOOGLE_CALENDAR_ID is not set", file=sys.stderr)
        return 2

    credentials, _ = google.auth.default(scopes=SCOPES)
    print(f"Authenticating as: {_identity(credentials)}")
    print(f"Target calendar:   {calendar_id}")

    events = build("calendar", "v3", credentials=credentials, static_discovery=True).events()

    try:
        events.list(calendarId=calendar_id, maxResults=1).execute()
    except HttpError as exc:
        print(_explain(exc, calendar_id), file=sys.stderr)
        return 1
    print("Read access:  OK")

    start = datetime.now(timezone.utc) - timedelta(days=365)
    probe_body = {
        "summary": PROBE_SUMMARY,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(minutes=15)).isoformat()},
    }
    try:
        probe = events.insert(calendarId=calendar_id, body=probe_body).execute()
    except HttpError as exc:
        print(_explain(exc, calendar_id), file=sys.stderr)
        return 1
    print("Write access: OK")

    try:
        events.delete(calendarId=calendar_id, eventId=probe["id"]).execute()
    except HttpError as exc:
        print(
            f"WARNING: could not clean up probe event {probe['id']} ({exc}). Delete it by hand.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
