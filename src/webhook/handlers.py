"""
Per-context handlers. Each maps an Amilia webhook payload to a Google
Calendar operation. main.py calls every handler with the same full set of
keyword arguments (action, payload, calendar_client, event_store, org_id,
amilia_client, task_queue); a handler that doesn't need some of them just
absorbs the rest via **_ignored rather than declaring every parameter.
"""

from __future__ import annotations

from amilia_client import AmiliaClient
from reconcile_queue import ReconcileQueue
from shared.calendar_client import CalendarClient
from shared.event_store import EventStore


# ---------------------------------------------------------------------------
# FacilityBooking: tool bookings, room bookings, and other facility bookings
# ---------------------------------------------------------------------------

def handle_facility_booking(
    action: str, payload: dict, calendar_client: CalendarClient, event_store: EventStore, **_ignored
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
            "FacilityBooking",
            reservation_id,
            {"calendar_event_id": event["id"], "action": "Create"},
            custom_time=payload["End"],
        )
        return {"reservation_id": reservation_id, "calendar_event_id": event["id"]}

    if action == "Update":
        existing = event_store.get("FacilityBooking", reservation_id)
        if existing is None:
            return {"reservation_id": reservation_id, "skipped": "no stored event mapping"}
        event_id = existing["calendar_event_id"]
        calendar_client.update_event(
            event_id,
            summary=payload.get("Title", "Facility Booking"),
            start_iso=payload["Start"],
            end_iso=payload["End"],
            location=(payload.get("Location") or {}).get("Name"),
        )
        event_store.set(
            "FacilityBooking",
            reservation_id,
            {"calendar_event_id": event_id, "action": "Update"},
            custom_time=payload["End"],
        )
        return {"reservation_id": reservation_id, "calendar_event_id": event_id, "updated": True}

    if action == "Delete":
        deleted_id = payload.get("Id")
        existing = event_store.get("FacilityBooking", deleted_id)
        if existing is None:
            return {"reservation_id": deleted_id, "skipped": "no stored event mapping"}
        calendar_client.delete_event(existing["calendar_event_id"])
        event_store.delete("FacilityBooking", deleted_id)
        return {"reservation_id": deleted_id, "deleted": True}

    return {"skipped": f"unhandled action {action}"}


# ---------------------------------------------------------------------------
# Program: a container of related Activities (e.g. a course). Never itself
# a calendar event — only its Online flag, gating its children, matters.
# ---------------------------------------------------------------------------

def handle_program(
    action: str,
    payload: dict,
    calendar_client: CalendarClient,
    event_store: EventStore,
    task_queue: ReconcileQueue,
    **_ignored,
) -> dict:
    program_id = payload.get("Id")

    if action == "Create":
        online = payload.get("Online", True)
        event_store.set(
            "Program",
            program_id,
            {
                "name": payload.get("Name"),
                "online": online,
                "start_date": payload.get("StartDate"),
                "expiration_date": payload.get("ExpirationDate"),
            },
        )
        # Catches Activities whose own Create webhook arrived before this
        # Program's did — normally a no-op (no children yet).
        _enqueue_reconciliation(event_store, task_queue, program_id)
        return {"program_id": program_id, "online": online}

    if action == "Update":
        previous = event_store.get("Program", program_id)
        was_online = previous.get("online") if previous else None
        online = payload.get("Online", True)
        event_store.set(
            "Program",
            program_id,
            {
                "name": payload.get("Name"),
                "online": online,
                "start_date": payload.get("StartDate"),
                "expiration_date": payload.get("ExpirationDate"),
            },
        )
        if online != was_online:
            _enqueue_reconciliation(event_store, task_queue, program_id)
        return {"program_id": program_id, "online": online, "updated": True}

    if action == "Delete":
        # Amilia fires Delete when a program is archived. Cascade: every
        # child Activity's calendar events and own document go too — Amilia
        # does not separately notify us about each child.
        activity_ids = event_store.list_members("Program", program_id, "Activities")
        for activity_id in activity_ids:
            activity = event_store.get("Activity", activity_id)
            if activity:
                _delete_activity_events(calendar_client, activity)
            event_store.delete("Activity", activity_id)
            event_store.remove_membership("Program", program_id, "Activities", activity_id)
        event_store.delete("Program", program_id)
        return {"program_id": program_id, "deleted": True, "activities_removed": len(activity_ids)}

    return {"skipped": f"unhandled action {action}"}


def _enqueue_reconciliation(event_store: EventStore, task_queue: ReconcileQueue, program_id) -> None:
    """
    Reconciles every known child of this Program against its current
    visibility. A Program's activity list can realistically run into the
    low hundreds, so each child is one small async task rather than work
    done inline here — a synchronous loop risks the webhook response
    taking long enough that Amilia (or our own Cloud Function timeout)
    gives up and retries mid-fan-out.
    """
    for activity_id in event_store.list_members("Program", program_id, "Activities"):
        task_queue.enqueue_reconciliation(activity_id)


def _delete_activity_events(calendar_client: CalendarClient, activity: dict) -> None:
    """Deletes every calendar event currently on record for this activity's occurrences."""
    for occurrence in (activity.get("occurrences") or {}).values():
        calendar_event_id = occurrence.get("calendar_event_id")
        if calendar_event_id:
            calendar_client.delete_event(calendar_event_id)


# ---------------------------------------------------------------------------
# Activity: the actual class/session offering, and a real calendar event —
# or rather, one calendar event per occurrence, since a single Activity can
# have multiple sessions (a one-off date, a recurring series, or both).
# Visible on the calendar only when its own Status is "Normal" AND its
# parent Program's Online flag is true (Program unknown = assumed online).
# ---------------------------------------------------------------------------

def handle_activity(
    action: str,
    payload: dict,
    calendar_client: CalendarClient,
    event_store: EventStore,
    org_id,
    amilia_client: AmiliaClient,
    **_ignored,
) -> dict:
    if action in ("Create", "Update"):
        activity_id = payload.get("Id")
        program_id = payload.get("ProgramId") or None  # Amilia uses 0 for "no program"
        status = payload.get("Status")

        program = event_store.get("Program", program_id) if program_id else None
        program_online = True if program is None else program.get("online", True)
        visible = program_online and status == "Normal"

        # Per-occurrence Start/End/Location isn't in the webhook payload at
        # all (only a human-readable ScheduleSummary) — this is the one
        # piece of information genuinely missing from the webhook, so it's
        # the only REST call this handler makes, once per Create/Update.
        occurrences = amilia_client.get_activity_occurrences(org_id, activity_id)

        existing = event_store.get("Activity", activity_id)
        existing_occurrences = (existing or {}).get("occurrences") or {}

        location = payload.get("LocationLabel") or None
        summary = payload.get("Name", "Activity")

        occurrence_map = {}
        fresh_ids = set()
        for occurrence in occurrences:
            occurrence_id = str(occurrence["Id"])
            fresh_ids.add(occurrence_id)
            previous = existing_occurrences.get(occurrence_id) or {}
            calendar_event_id = previous.get("calendar_event_id")

            if visible:
                if calendar_event_id:
                    calendar_client.update_event(
                        calendar_event_id,
                        summary=summary,
                        start_iso=occurrence["Start"],
                        end_iso=occurrence["End"],
                        location=location,
                    )
                else:
                    event = calendar_client.create_event(
                        summary=summary,
                        start_iso=occurrence["Start"],
                        end_iso=occurrence["End"],
                        location=location,
                    )
                    calendar_event_id = event["id"]
            elif calendar_event_id:
                calendar_client.delete_event(calendar_event_id)
                calendar_event_id = None

            # start/end are stored (not just calendar_event_id) so a later
            # Program-triggered reconciliation can create/recreate this
            # occurrence's event without another REST call — see
            # src/reconciler/main.py.
            occurrence_map[occurrence_id] = {
                "start": occurrence["Start"],
                "end": occurrence["End"],
                "calendar_event_id": calendar_event_id,
            }

        # Occurrences that used to exist but dropped out of the fresh list
        # (the schedule shrank) still need their calendar events removed.
        for occurrence_id, previous in existing_occurrences.items():
            if occurrence_id not in fresh_ids:
                calendar_event_id = (previous or {}).get("calendar_event_id")
                if calendar_event_id:
                    calendar_client.delete_event(calendar_event_id)

        old_program_id = (existing or {}).get("program_id")
        if old_program_id and old_program_id != program_id:
            event_store.remove_membership("Program", old_program_id, "Activities", activity_id)
        if program_id:
            event_store.add_membership("Program", program_id, "Activities", activity_id)

        event_store.set(
            "Activity",
            activity_id,
            {
                "name": payload.get("Name"),
                "program_id": program_id,
                "category_id": payload.get("CategoryId") or None,
                "category_name": payload.get("CategoryName"),
                "sub_category_id": payload.get("SubCategoryId") or None,
                "sub_category_name": payload.get("SubCategoryName"),
                "status": status,
                "start_date": payload.get("StartDate"),
                "end_date": payload.get("EndDate"),
                "location": location,
                "occurrences": occurrence_map,
            },
            custom_time=payload.get("EndDate"),
        )
        return {
            "activity_id": activity_id,
            "visible": visible,
            "occurrence_count": len(occurrence_map),
        }

    if action == "Delete":
        deleted_id = payload.get("Id")
        existing = event_store.get("Activity", deleted_id)
        if existing is None:
            return {"activity_id": deleted_id, "skipped": "no stored activity"}
        _delete_activity_events(calendar_client, existing)
        program_id = existing.get("program_id")
        if program_id:
            event_store.remove_membership("Program", program_id, "Activities", deleted_id)
        event_store.delete("Activity", deleted_id)
        return {"activity_id": deleted_id, "deleted": True}

    return {"skipped": f"unhandled action {action}"}
