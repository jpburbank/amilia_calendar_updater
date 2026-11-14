"""
Per-context handlers. Each maps an Amilia webhook payload to a Google
Calendar operation. main.py calls every handler with the same full set of
keyword arguments (action, payload, event_time, calendar_client,
event_store, org_id, amilia_client, task_queue); a handler that doesn't need
some of them just absorbs the rest via **_ignored rather than declaring
every parameter.

Stale deliveries: Amilia delivers at least once and can replay an old
webhook after a newer one (seen live 2026-11-13: an Activity Create was
redelivered six minutes later, after an Update, and reverted the rename).
Every stored document records the EventTime of the webhook that last wrote
it (last_event_time), and a webhook whose EventTime is not newer than that
is skipped, which also drops exact duplicate deliveries.

Current state: EventTime only orders webhooks against a stored document.
Once a record is deleted (or was never stored), a replayed old webhook has
nothing to compare against, so Activity and Program Create/Update also ask
Amilia for the record's current state. A 404 (deleted) or an archived
Program is skipped, and an Activity's visibility uses its current Status,
so an old "Normal" can't put a hidden or archived activity back on the
calendar. If that lookup fails for any other reason, the handler either
raises (fail_open=False: the background worker still has retries left, so
it retries for fresh state) or falls back to the webhook payload
(fail_open=True: the final attempt, or inline processing with no retries
behind it), so an Amilia outage never loses a change.
"""

from __future__ import annotations

import logging
from datetime import datetime

from amilia_client import AmiliaClient, AmiliaNotFound
from reconcile_queue import ReconcileQueue
from shared.calendar_client import CalendarClient, stable_event_id
from shared.event_store import EventStore
from shared.titles import clean_title

logger = logging.getLogger(__name__)


def _parse_event_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        logger.warning("unparseable EventTime %r; not checking for staleness", value)
        return None


def _stale_skip(existing: dict | None, event_time: str | None, **ids) -> dict | None:
    """
    Returns a "skipped" result when this webhook is no newer than the one
    that last wrote `existing`, else None. A missing or unparseable time on
    either side never counts as stale (fail open: process the webhook).
    """
    incoming = _parse_event_time(event_time)
    stored = _parse_event_time((existing or {}).get("last_event_time"))
    if incoming is None or stored is None or incoming > stored:
        return None
    return {
        **ids,
        "skipped": "stale event",
        "event_time": event_time,
        "last_event_time": existing["last_event_time"],
    }


def _fetch_current(fetch, description: str, fail_open: bool) -> dict | None:
    """
    Amilia's current copy of a record. If it can't be fetched: with
    fail_open, None (the caller falls back to the webhook payload);
    without, the error propagates so the task is retried.
    AmiliaNotFound always propagates, because a 404 is a real answer.
    """
    try:
        return fetch()
    except AmiliaNotFound:
        raise
    except Exception:
        if not fail_open:
            raise
        logger.warning(
            "could not fetch current %s from Amilia; using the webhook payload",
            description,
            exc_info=True,
        )
        return None


def _last_event_time(existing: dict | None, event_time: str | None) -> str | None:
    """The value to store: this webhook's EventTime, or the previous one if it has none."""
    if _parse_event_time(event_time) is not None:
        return event_time
    return (existing or {}).get("last_event_time")


# ---------------------------------------------------------------------------
# FacilityBooking: tool bookings, room bookings, and other facility bookings
#
# Dormant: only routed when FACILITY_BOOKING_SYNC_ENABLED is "true" (off by
# default — see shared/feature_flags.py and main.py's _build_handlers). Kept
# working and tested so it can be switched back on without a rewrite.
# ---------------------------------------------------------------------------


def handle_facility_booking(
    action: str,
    payload: dict,
    calendar_client: CalendarClient,
    event_store: EventStore,
    event_time: str | None = None,
    **_ignored,
) -> dict:
    reservation_id = payload.get("ReservationId")

    if action == "Create":
        existing = event_store.get("FacilityBooking", reservation_id)
        stale = _stale_skip(existing, event_time, reservation_id=reservation_id)
        if stale:
            return stale
        title = clean_title(payload.get("Title"), "Facility Booking")
        event = calendar_client.create_event(
            event_id=stable_event_id("FacilityBooking", reservation_id),
            summary=f"{title} booking",
            start_iso=payload["Start"],
            end_iso=payload["End"],
            location=(payload.get("Location") or {}).get("Name"),
            extended_properties={
                "amilia_type": "FacilityBooking",
                "amilia_type_id": str(reservation_id),
            },
        )
        event_store.set(
            "FacilityBooking",
            reservation_id,
            {
                "calendar_event_id": event["id"],
                "action": "Create",
                "last_event_time": _last_event_time(existing, event_time),
            },
            custom_time=payload["End"],
        )
        return {"reservation_id": reservation_id, "calendar_event_id": event["id"]}

    if action == "Update":
        existing = event_store.get("FacilityBooking", reservation_id)
        if existing is None:
            return {"reservation_id": reservation_id, "skipped": "no stored event mapping"}
        stale = _stale_skip(existing, event_time, reservation_id=reservation_id)
        if stale:
            return stale
        event_id = existing["calendar_event_id"]
        title = clean_title(payload.get("Title"), "Facility Booking")
        calendar_client.update_event(
            event_id,
            summary=f"{title} booking",
            start_iso=payload["Start"],
            end_iso=payload["End"],
            location=(payload.get("Location") or {}).get("Name"),
            extended_properties={
                "amilia_type": "FacilityBooking",
                "amilia_type_id": str(reservation_id),
            },
        )
        event_store.set(
            "FacilityBooking",
            reservation_id,
            {
                "calendar_event_id": event_id,
                "action": "Update",
                "last_event_time": _last_event_time(existing, event_time),
            },
            custom_time=payload["End"],
        )
        return {"reservation_id": reservation_id, "calendar_event_id": event_id, "updated": True}

    if action == "Delete":
        deleted_id = payload.get("Id")
        existing = event_store.get("FacilityBooking", deleted_id)
        if existing is None:
            return {"reservation_id": deleted_id, "skipped": "no stored event mapping"}
        stale = _stale_skip(existing, event_time, reservation_id=deleted_id)
        if stale:
            return stale
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
    org_id=None,
    amilia_client: AmiliaClient | None = None,
    event_time: str | None = None,
    fail_open: bool = True,
    **_ignored,
) -> dict:
    program_id = payload.get("Id")

    if action == "Create":
        previous = event_store.get("Program", program_id)
        stale = _stale_skip(previous, event_time, program_id=program_id)
        if stale:
            return stale
        gone = _program_gone(amilia_client, org_id, program_id, fail_open)
        if gone:
            return gone
        online = payload.get("Online", True)
        event_store.set(
            "Program",
            program_id,
            {
                "name": clean_title(payload.get("Name")),
                "online": online,
                "start_date": payload.get("StartDate"),
                "expiration_date": payload.get("ExpirationDate"),
                "last_event_time": _last_event_time(previous, event_time),
            },
        )
        # Catches Activities whose own Create webhook arrived before this
        # Program's did — normally a no-op (no children yet).
        _enqueue_reconciliation(event_store, task_queue, program_id)
        return {"program_id": program_id, "online": online}

    if action == "Update":
        previous = event_store.get("Program", program_id)
        stale = _stale_skip(previous, event_time, program_id=program_id)
        if stale:
            return stale
        gone = _program_gone(amilia_client, org_id, program_id, fail_open)
        if gone:
            return gone
        was_online = previous.get("online") if previous else None
        online = payload.get("Online", True)
        event_store.set(
            "Program",
            program_id,
            {
                "name": clean_title(payload.get("Name")),
                "online": online,
                "start_date": payload.get("StartDate"),
                "expiration_date": payload.get("ExpirationDate"),
                "last_event_time": _last_event_time(previous, event_time),
            },
        )
        if online != was_online:
            _enqueue_reconciliation(event_store, task_queue, program_id)
        return {"program_id": program_id, "online": online, "updated": True}

    if action == "Delete":
        # Amilia fires Delete when a program is archived. Cascade: every
        # child Activity's calendar events and own document go too — Amilia
        # does not separately notify us about each child.
        stale = _stale_skip(
            event_store.get("Program", program_id), event_time, program_id=program_id
        )
        if stale:
            return stale
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


def _program_gone(amilia_client: AmiliaClient, org_id, program_id, fail_open: bool) -> dict | None:
    """A "skipped" result if Amilia says the Program is deleted or archived, else None."""
    try:
        current = _fetch_current(
            lambda: amilia_client.get_program(org_id, program_id),
            f"Program {program_id}",
            fail_open,
        )
    except AmiliaNotFound:
        return {"program_id": program_id, "skipped": "deleted in Amilia"}
    if current and current.get("IsArchived"):
        return {"program_id": program_id, "skipped": "archived in Amilia"}
    return None


def _enqueue_reconciliation(
    event_store: EventStore, task_queue: ReconcileQueue, program_id
) -> None:
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


def _build_activity_description(payload: dict) -> str | None:
    """
    Builds the calendar event description from Description/Note/Prerequisite,
    each its own paragraph. Description has no header (Amilia already sends
    it as its own HTML paragraph(s)); Note and Prerequisite each get an HTML
    header matching that same style, and are omitted entirely when null or
    empty rather than shown blank.
    """
    paragraphs = []
    description = payload.get("Description")
    if description:
        paragraphs.append(description)
    note = payload.get("Note")
    if note:
        paragraphs.append(f"<p><strong>Note</strong><br>{note}</p>")
    prerequisite = payload.get("Prerequisite")
    if prerequisite:
        paragraphs.append(f"<p><strong>Prerequisite</strong><br>{prerequisite}</p>")
    return "\n".join(paragraphs) if paragraphs else None


def handle_activity(
    action: str,
    payload: dict,
    calendar_client: CalendarClient,
    event_store: EventStore,
    org_id,
    amilia_client: AmiliaClient,
    event_time: str | None = None,
    fail_open: bool = True,
    **_ignored,
) -> dict:
    if action in ("Create", "Update"):
        activity_id = payload.get("Id")
        # Checked before the REST call below, so a stale replay costs nothing.
        existing = event_store.get("Activity", activity_id)
        stale = _stale_skip(existing, event_time, activity_id=activity_id)
        if stale:
            return stale
        program_id = payload.get("ProgramId") or None  # Amilia uses 0 for "no program"

        # Per-occurrence Start/End/Location isn't in the webhook payload at
        # all (only a human-readable ScheduleSummary), so it always comes
        # from the REST API. The Activity itself is fetched too, for its
        # current Status (see the module docstring).
        try:
            current = _fetch_current(
                lambda: amilia_client.get_activity(org_id, activity_id),
                f"Activity {activity_id}",
                fail_open,
            )
            occurrences = amilia_client.get_activity_occurrences(org_id, activity_id)
        except AmiliaNotFound:
            return {"activity_id": activity_id, "skipped": "deleted in Amilia"}
        status = (current or payload).get("Status")

        program = event_store.get("Program", program_id) if program_id else None
        program_online = True if program is None else program.get("online", True)
        visible = program_online and status == "Normal"

        existing_occurrences = (existing or {}).get("occurrences") or {}

        location = payload.get("LocationLabel") or None
        summary = clean_title(payload.get("Name"), "Activity")
        description = _build_activity_description(payload)
        extended_properties = {"amilia_type": "Activity", "amilia_type_id": str(activity_id)}

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
                        description=description,
                        extended_properties=extended_properties,
                    )
                else:
                    event = calendar_client.create_event(
                        event_id=stable_event_id("Activity", activity_id, occurrence_id),
                        summary=summary,
                        start_iso=occurrence["Start"],
                        end_iso=occurrence["End"],
                        location=location,
                        description=description,
                        extended_properties=extended_properties,
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
                "name": clean_title(payload.get("Name")),
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
                "last_event_time": _last_event_time(existing, event_time),
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
        stale = _stale_skip(existing, event_time, activity_id=deleted_id)
        if stale:
            return stale
        _delete_activity_events(calendar_client, existing)
        program_id = existing.get("program_id")
        if program_id:
            event_store.remove_membership("Program", program_id, "Activities", deleted_id)
        event_store.delete("Activity", deleted_id)
        return {"activity_id": deleted_id, "deleted": True}

    return {"skipped": f"unhandled action {action}"}
