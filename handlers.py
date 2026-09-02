"""
Per-context handlers. Each maps an Amilia webhook payload to a Google
Calendar operation.

NOTE: Create/Update/Delete all need a way to look up "which calendar
event corresponds to this ReservationId/RegistrationId". That lookup
is intentionally left as a TODO (no persistence layer is scaffolded
here) — plug in whatever store you choose at the marked spots.
"""

from __future__ import annotations

from calendar_client import CalendarClient


# ---------------------------------------------------------------------------
# FacilityBooking: tool bookings, room bookings, and other facility bookings
# ---------------------------------------------------------------------------

def handle_facility_booking(action: str, payload: dict, calendar_client: CalendarClient) -> dict:
    reservation_id = payload.get("ReservationId")

    if action == "Create":
        event = calendar_client.create_event(
            summary=payload.get("Title", "Facility Booking"),
            start_iso=payload["Start"],
            end_iso=payload["End"],
            location=(payload.get("Location") or {}).get("Name"),
        )
        # TODO: persist mapping reservation_id -> event["id"]
        return {"reservation_id": reservation_id, "calendar_event_id": event["id"]}

    if action == "Update":
        # TODO: look up calendar_event_id for reservation_id
        event_id = _lookup_event_id(reservation_id)
        if event_id is None:
            return {"reservation_id": reservation_id, "skipped": "no stored event mapping"}
        calendar_client.update_event(
            event_id,
            summary=payload.get("Title", "Facility Booking"),
            start_iso=payload["Start"],
            end_iso=payload["End"],
            location=(payload.get("Location") or {}).get("Name"),
        )
        return {"reservation_id": reservation_id, "calendar_event_id": event_id, "updated": True}

    if action == "Delete":
        deleted_id = payload.get("Id")
        # TODO: look up calendar_event_id for deleted_id
        event_id = _lookup_event_id(deleted_id)
        if event_id is None:
            return {"reservation_id": deleted_id, "skipped": "no stored event mapping"}
        calendar_client.delete_event(event_id)
        # TODO: remove stored mapping for deleted_id
        return {"reservation_id": deleted_id, "deleted": True}

    return {"skipped": f"unhandled action {action}"}


# ---------------------------------------------------------------------------
# Registration: class / activity registrations (includes drop-ins, private
# lessons, multipass redemptions)
# ---------------------------------------------------------------------------

def handle_registration(action: str, payload: dict, calendar_client: CalendarClient) -> dict:
    registration_id = payload.get("RegistrationId")

    if action == "Create":
        activity = payload.get("Activity", {})
        drop_in = payload.get("DropIn") or {}

        # NOTE: the Registration payload does not include explicit
        # Start/End times for scheduled (non-drop-in) sessions — only
        # DropIn.OccurrenceDate for drop-ins. For recurring session
        # registrations you'll need a supplementary call to the
        # Activity/schedule API to resolve real occurrence start/end
        # times. Left as a TODO — placeholder below only handles the
        # drop-in case.
        start_iso = drop_in.get("OccurrenceDate")
        if start_iso is None:
            return {
                "registration_id": registration_id,
                "skipped": "no occurrence time in payload; needs Activity schedule lookup",
            }

        event = calendar_client.create_event(
            summary=activity.get("Name", "Class Registration"),
            start_iso=start_iso,
            end_iso=start_iso,  # TODO: derive real end time from activity duration
        )
        # TODO: persist mapping registration_id -> event["id"]
        return {"registration_id": registration_id, "calendar_event_id": event["id"]}

    if action == "Update":
        # TODO: look up calendar_event_id for registration_id, then patch as needed
        return {"registration_id": registration_id, "skipped": "update not yet implemented"}

    if action == "Delete":
        # TODO: look up calendar_event_id for registration_id and delete it
        return {"registration_id": registration_id, "skipped": "delete not yet implemented"}

    return {"skipped": f"unhandled action {action}"}


def _lookup_event_id(external_id: str) -> str | None:
    """Placeholder for external_id -> Google Calendar event ID lookup."""
    return None
