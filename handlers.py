"""
Per-context handlers. Each maps an Amilia webhook payload to a Google
Calendar operation.
"""

from __future__ import annotations

from calendar_client import CalendarClient
from event_store import EventStore


# ---------------------------------------------------------------------------
# FacilityBooking: tool bookings, room bookings, and other facility bookings
# ---------------------------------------------------------------------------

def handle_facility_booking(
    action: str, payload: dict, calendar_client: CalendarClient, event_store: EventStore
) -> dict:
    reservation_id = payload.get("ReservationId")

    if action == "Create":
        title = payload.get("Title", "Facility Booking")
        event = calendar_client.create_event(
            summary=f"{title} booking",
            start_iso=payload["Start"],
            end_iso=payload["End"],
            location=(payload.get("Location") or {}).get("Name"),
        )
        event_store.set(
            "FacilityBooking", reservation_id, event["id"], action="Create", end_iso=payload["End"]
        )
        return {"reservation_id": reservation_id, "calendar_event_id": event["id"]}

    if action == "Update":
        event_id = event_store.get("FacilityBooking", reservation_id)
        if event_id is None:
            return {"reservation_id": reservation_id, "skipped": "no stored event mapping"}
        calendar_client.update_event(
            event_id,
            summary=payload.get("Title", "Facility Booking"),
            start_iso=payload["Start"],
            end_iso=payload["End"],
            location=(payload.get("Location") or {}).get("Name"),
        )
        event_store.set(
            "FacilityBooking", reservation_id, event_id, action="Update", end_iso=payload["End"]
        )
        return {"reservation_id": reservation_id, "calendar_event_id": event_id, "updated": True}

    if action == "Delete":
        deleted_id = payload.get("Id")
        event_id = event_store.get("FacilityBooking", deleted_id)
        if event_id is None:
            return {"reservation_id": deleted_id, "skipped": "no stored event mapping"}
        calendar_client.delete_event(event_id)
        event_store.delete("FacilityBooking", deleted_id)
        return {"reservation_id": deleted_id, "deleted": True}

    return {"skipped": f"unhandled action {action}"}
