"""
Thin wrapper around the Google Calendar API v3.

Auth model: the Cloud Function's runtime service account, via Application
Default Credentials. The service account owns no calendars of its own — it
can only touch a calendar whose owner has explicitly shared it.

Setup (done once per calendar, by that calendar's owner):

  1. Enable the API on the project:
       gcloud services enable calendar-json.googleapis.com

  2. Find the function's runtime service account:
       gcloud functions describe amilia-calendar-sync --gen2 --region=<REGION> \
         --format='value(serviceConfig.serviceAccount)'

  3. In Google Calendar, open Settings for the target calendar ->
     "Share with specific people or groups" -> Add people -> paste that
     email -> permission "Make changes to events".

  4. Verify:  GOOGLE_CALENDAR_ID=... python check_access.py

Step 3 has to happen in the Calendar UI: an unshared service account has no
standing to grant itself access, so there is no API path that bootstraps it.
"""

from __future__ import annotations

from typing import Optional

import google.auth
import google.auth.exceptions
import httplib2
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Narrowest scope that covers create/patch/delete on a calendar shared with
# us. Deliberately not the full "calendar" scope, which would also permit
# creating calendars and rewriting sharing rules.
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

# Statuses worth asking Amilia to redeliver for. 403 and 404 are in here
# because under this auth model they are the expected symptom of "the
# calendar has not been shared with us yet" or "the share was revoked" —
# operator-fixable, and Amilia's retry window is the grace period to fix
# them and have the missed bookings land on their own.
RETRYABLE_STATUSES = frozenset({403, 404, 429, 500, 502, 503, 504})

_TRANSPORT_ERRORS = (
    google.auth.exceptions.TransportError,
    httplib2.HttpLib2Error,
    ConnectionError,
    TimeoutError,
)


def is_retryable(exc: BaseException) -> bool:
    """
    Whether redelivering the webhook could plausibly succeed.

    Anything else — a 400 from Google, a missing field in the payload, a bug
    in a handler — produces the identical failure on every redelivery, and
    Amilia disables a subscription after ~72h of failures. Those must be
    acked and alerted on instead of retried.
    """
    if isinstance(exc, HttpError):
        return exc.resp.status in RETRYABLE_STATUSES
    if isinstance(exc, _TRANSPORT_ERRORS):
        return True
    return False


class CalendarClient:
    def __init__(self, calendar_id: str, credentials=None):
        self.calendar_id = calendar_id
        if credentials is None:
            credentials, _ = google.auth.default(scopes=SCOPES)
        self._service = build(
            "calendar",
            "v3",
            credentials=credentials,
            # Use the discovery document bundled with the library rather than
            # fetching it over the network on every cold start.
            static_discovery=True,
        )

    def create_event(
        self,
        summary: str,
        start_iso: str,
        end_iso: str,
        location: Optional[str] = None,
        description: Optional[str] = None,
    ) -> dict:
        """Creates an event and returns the created event resource (contains 'id')."""
        body = {
            "summary": summary,
            "start": {"dateTime": start_iso},
            "end": {"dateTime": end_iso},
        }
        if location:
            body["location"] = location
        if description:
            body["description"] = description

        return self._service.events().insert(calendarId=self.calendar_id, body=body).execute()

    def update_event(self, event_id: str, **fields) -> dict:
        """Patches an existing event. fields may include summary/start/end/location/description."""
        body = {}
        if "summary" in fields:
            body["summary"] = fields["summary"]
        if "start_iso" in fields:
            body["start"] = {"dateTime": fields["start_iso"]}
        if "end_iso" in fields:
            body["end"] = {"dateTime": fields["end_iso"]}
        if "location" in fields:
            body["location"] = fields["location"]
        if "description" in fields:
            body["description"] = fields["description"]

        return self._service.events().patch(
            calendarId=self.calendar_id, eventId=event_id, body=body
        ).execute()

    def delete_event(self, event_id: str) -> None:
        self._service.events().delete(calendarId=self.calendar_id, eventId=event_id).execute()
