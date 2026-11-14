"""
Thin wrapper around the Google Calendar API v3.

Auth model: the Cloud Function's runtime service account, via Application
Default Credentials. The service account owns no calendars of its own — it
can only touch a calendar whose owner has explicitly shared it.

Setup (done once per calendar, by that calendar's owner):

  1. Enable the API on the project:
       gcloud services enable calendar-json.googleapis.com

  2. Find the function's runtime service account:
       gcloud functions describe amilia-calendar-updater --gen2 --region=<REGION> \
         --format='value(serviceConfig.serviceAccount)'

  3. In Google Calendar, open Settings for the target calendar ->
     "Share with specific people or groups" -> Add people -> paste that
     email -> permission "Make changes to events".

  4. Verify:  GOOGLE_CALENDAR_ID=... PYTHONPATH=src .venv/bin/python scripts/check_access.py

Step 3 has to happen in the Calendar UI: an unshared service account has no
standing to grant itself access, so there is no API path that bootstraps it.
"""

from __future__ import annotations

import base64
import json
import logging

import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# Narrowest scope that covers create/patch/delete on a calendar shared with
# us. Deliberately not the full "calendar" scope, which would also permit
# creating calendars and rewriting sharing rules.
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def stable_event_id(*parts) -> str:
    """
    A stable Google Calendar event ID derived from Amilia IDs, e.g.
    stable_event_id("Activity", 7482865, 145842706). Creating an event
    under a known ID makes a retried create safe: the second insert gets a
    409 instead of a duplicate event (see create_event). Calendar requires
    5-1024 characters from base32hex (0-9, a-v), which the lowercased,
    unpadded base32hex encoding of the parts always satisfies.
    """
    raw = "/".join(str(part) for part in parts).encode()
    return base64.b32hexencode(raw).decode().rstrip("=").lower()


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
        location: str | None = None,
        description: str | None = None,
        extended_properties: dict | None = None,
        event_id: str | None = None,
    ) -> dict:
        """
        Creates an event and returns the created event resource (contains
        'id').

        With `event_id` (see stable_event_id), creating is idempotent: if
        that ID already exists, live or deleted (Calendar keeps deleted
        events as status "cancelled", so their IDs can't be reused), the
        existing event is patched with these fields and status "confirmed"
        instead. That restores a deleted event rather than failing, so an
        activity hidden and later shown again reuses its event. Verified
        live 2026-11-13: re-insert gives 409, and patching a cancelled
        event to "confirmed" restores it.

        `extended_properties` (a flat string->string dict) is stored
        as the event's extendedProperties.private — visible via the API to
        any caller with read access to the event, not restricted to this
        app; "private" here only means it doesn't propagate to other
        attendees' copies of the event, and it's never shown in the
        Calendar UI either way.
        """
        body = {
            "summary": summary,
            "start": {"dateTime": start_iso},
            "end": {"dateTime": end_iso},
        }
        if location:
            body["location"] = location
        if description:
            body["description"] = description
        if extended_properties:
            body["extendedProperties"] = {"private": extended_properties}
        if event_id is None:
            event = self._service.events().insert(calendarId=self.calendar_id, body=body).execute()
            _log_response("insert", None, event)
            return event

        try:
            event = (
                self._service.events()
                .insert(calendarId=self.calendar_id, body={**body, "id": event_id})
                .execute()
            )
            _log_response("insert", event_id, event)
            return event
        except HttpError as exc:
            if exc.resp.status != 409:
                raise
        event = (
            self._service.events()
            .patch(
                calendarId=self.calendar_id,
                eventId=event_id,
                body={**body, "status": "confirmed"},
            )
            .execute()
        )
        _log_response("patch", event_id, event)
        return event

    def update_event(self, event_id: str, **fields) -> dict:
        """
        Patches an existing event. fields may include
        summary/start_iso/end_iso/location/description/extended_properties
        (see create_event for what extended_properties means).
        """
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
        if "extended_properties" in fields:
            body["extendedProperties"] = {"private": fields["extended_properties"]}

        event = (
            self._service.events()
            .patch(calendarId=self.calendar_id, eventId=event_id, body=body)
            .execute()
        )
        _log_response("patch", event_id, event)
        return event

    def delete_event(self, event_id: str) -> None:
        response = (
            self._service.events().delete(calendarId=self.calendar_id, eventId=event_id).execute()
        )
        _log_response("delete", event_id, response)


def _log_response(operation: str, event_id: str | None, response) -> None:
    """
    TEMP DEBUG — remove before merging. Logs every Google Calendar API
    response verbatim (delete returns an empty body). Filter with:
    textPayload:"GOOGLE_CALENDAR_RESPONSE"
    """
    logger.info(
        "GOOGLE_CALENDAR_RESPONSE operation=%s event_id=%s response=%s",
        operation,
        event_id,
        json.dumps(response),
    )
