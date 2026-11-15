"""
One-time bulk loader: seeds the event store (and creates matching calendar
events) for Programs, Activities, and FacilityBookings that already existed
in Amilia before this integration's webhook subscriptions did. Webhooks
only deliver changes going forward — without this, anything created before
those subscriptions existed would only be discovered by the fail-open
"Program unknown -> assumed visible" path (for Programs), or never
discovered at all (for Activities and FacilityBookings, which have no
equivalent fallback).

FacilityBooking here means everything the live webhook receives under that
same Context, with one deliberate exception. Amilia's /reservations
endpoint returns four Types (AdminBooking, Activity, FacilityBooking,
PrivateLesson, with ReservationId prefixes AB-/AC-/FB-/PL- respectively); a
reservations Type of "Activity" is a booked *session* (someone reserved a
slot), a different concept from this script's own Activity backfill above,
which seeds the Activity's own definition and occurrence schedule via
get_program_activities()/get_activity_occurrences() — but the *session*
itself is still something this script would otherwise also create a
calendar event for, duplicating the one the Activity backfill already
created for that same occurrence. So Type == "Activity" reservations are
skipped here (see backfill_facility_booking); AdminBooking, FacilityBooking,
and PrivateLesson are imported as before.

The whole FacilityBooking phase only runs when FACILITY_BOOKING_SYNC_ENABLED
is "true" (read from the environment, same as the live webhook; off by
default, see src/shared/feature_flags.py). With it off, the script still
backfills Programs and Activities and skips the /reservations fetch
entirely.

Does not import anything from src/webhook/ (that package is specifically
the Cloud Function's own code, including a Cloud Tasks dependency this
script has no use for). Everything it shares with the functions lives in
src/shared/: the Amilia REST client, the calendar and store clients, and
the rules for what an Activity's events and stored record contain, so the
backfill and live sync can't drift apart.

Run once, impersonating the function's own service account identity (the
same identity real traffic runs as, so this write exercises the same IAM
path production does — see scripts/check_access.py for why):
    gcloud auth application-default login \
      --impersonate-service-account=<SA_EMAIL>

    GOOGLE_CALENDAR_ID=$(gcloud secrets versions access latest --secret=google-calendar-id) \
    GOOGLE_EVENT_STORE_BUCKET=... \
      PYTHONPATH=src .venv/bin/python scripts/backfill.py \
      --org-id <ORG_ID> --amilia-username <user> --amilia-password <pass>

Add --dry-run first to preview what would happen without writing anything.

Safe to re-run: an occurrence that already has a stored calendar_event_id
is left alone rather than duplicated.

--update-existing also brings existing Activity events up to date (e.g. to
add the per-occurrence location to events created before it was synced):
each existing event is read, and only the fields that differ from Amilia
are patched. It never deletes or recreates an event, and never touches a
hidden Activity or one whose Program is offline. Anything edited by hand on
the calendar in those fields is overwritten, so run it with --dry-run
first: that prints every field that would change, e.g.
    event 85hn... location: None -> 'Wood shop'
without writing anything.

Scope: every Program (there are only ever a handful). Every Activity whose
EndDate is not more than --lookback-days in the past (default 30) — no
upper/forward bound, so anything upcoming is always included regardless of
how far out it starts. An Activity that ended further back than that is
treated as no longer relevant and skipped.

Every FacilityBooking whose Start falls between --lookback-days in the past
and --reservation-lookahead-days in the future (default 730, i.e. ~2 years).
Unlike Activities, Amilia's /reservations endpoint requires an explicit date
range server-side — it silently returns only *today's* reservations if none
is given — so there's no way to ask for a truly unbounded forward window the
way get_programs()/get_program_activities() allow; --reservation-lookahead-days
is a practical stand-in. A cancelled reservation (IsCancelled) is skipped
outright: there's no value in creating a calendar event for a slot nobody
will use. A reservation whose Type is "Activity" is also skipped — it's a
booked session of an Activity, not a standalone facility use, and already
fully covered by the Activity backfill above; importing it here too would
create a second, duplicate calendar event for the same real-world session
(confirmed in production: every Activity occurrence was getting both its
own event and a second "<name> booking" event from this path).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import UTC, datetime, timedelta

import google.auth
import google.auth.transport.requests

from shared.activities import activity_event_fields, activity_record, stored_occurrence
from shared.amilia_client import AmiliaClient
from shared.calendar_client import CalendarClient, stable_event_id
from shared.event_store import EventStore
from shared.feature_flags import facility_booking_sync_enabled
from shared.titles import clean_title

REQUEST_PACING_SECONDS = 0.2  # be gentle on Amilia's API


def _identity() -> str:
    """Best-effort email for whoever we authenticated to GCP as."""
    credentials, _ = google.auth.default()
    email = getattr(credentials, "service_account_email", None)
    if not email or email == "default":
        credentials.refresh(google.auth.transport.requests.Request())
        email = getattr(credentials, "service_account_email", None)
    return email or "<not a service account — probably your user account>"


def program_is_online(program: dict) -> bool:
    """REST Programs use IsVisible, not the webhook payload's Online field."""
    return program.get("IsVisible", True)


def is_within_lookback(activity: dict, cutoff: datetime) -> bool:
    """
    True if this activity's last occurrence ended on/after `cutoff` — which,
    since `cutoff` is always in the past, is also true for every activity
    entirely in the future. An activity with no EndDate at all is kept
    rather than silently dropped.
    """
    end_date = activity.get("EndDate")
    if not end_date:
        return True
    return datetime.fromisoformat(end_date) >= cutoff


def backfill_program(event_store: EventStore, program: dict, *, dry_run: bool) -> bool:
    """Writes the Program's document. Returns the resolved online flag."""
    online = program_is_online(program)
    print(f"  Program {program['Id']} {program.get('Name')!r}: online={online}")
    if not dry_run:
        event_store.set(
            "Program",
            program["Id"],
            {
                "name": clean_title(program.get("Name")),
                "online": online,
                "start_date": program.get("Start"),
                "expiration_date": program.get("End"),
            },
        )
    return online


def event_changes(event: dict, fields: dict) -> dict:
    """
    {field: (calendar value, wanted value)} for every field of `fields` (as
    built by activity_event_fields) that the calendar event doesn't already
    match. Times are compared as instants, since the calendar returns them in
    UTC; extended properties only need to contain the wanted keys.
    """
    current = {
        "summary": event.get("summary"),
        "start_iso": (event.get("start") or {}).get("dateTime"),
        "end_iso": (event.get("end") or {}).get("dateTime"),
        "location": event.get("location"),
        "description": event.get("description"),
        "extended_properties": (event.get("extendedProperties") or {}).get("private") or {},
    }
    changes = {}
    for name, wanted in fields.items():
        have = current[name]
        if name in ("start_iso", "end_iso"):
            same = have is not None and _instant(have) == _instant(wanted)
        elif name == "extended_properties":
            same = all(have.get(key) == value for key, value in wanted.items())
        else:
            same = (have or None) == (wanted or None)
        if not same:
            changes[name] = (have, wanted)
    return changes


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def backfill_activity(
    calendar_client: CalendarClient,
    event_store: EventStore,
    amilia: AmiliaClient,
    org_id,
    activity: dict,
    *,
    program_online: bool,
    dry_run: bool,
    update_existing: bool = False,
) -> None:
    """
    Creates a calendar event for every visible occurrence that has none. With
    update_existing, also patches existing events whose title, times,
    location, description or Amilia tags differ from Amilia's; it never
    deletes or recreates one (a missing or cancelled event is reported and
    left alone, since removing and repopulating events is done by hand).
    """
    activity_id = activity["Id"]
    status = activity.get("Status")
    visible = program_online and status == "Normal"

    existing = event_store.get("Activity", activity_id)
    existing_occurrences = (existing or {}).get("occurrences") or {}

    occurrences = amilia.get_activity_occurrences(org_id, activity_id)
    record = activity_record(activity, status)

    occurrence_map = {}
    counts = {"created": 0, "updated": 0, "unchanged": 0, "missing": 0}
    for occurrence in occurrences:
        occurrence_id = str(occurrence["Id"])
        previous = existing_occurrences.get(occurrence_id) or {}
        calendar_event_id = previous.get("calendar_event_id")
        stored = stored_occurrence(occurrence, activity)
        fields = activity_event_fields(activity_id, record, stored)

        if visible and not calendar_event_id:
            if not dry_run:
                event = calendar_client.create_event(
                    event_id=stable_event_id("Activity", activity_id, occurrence_id), **fields
                )
                calendar_event_id = event["id"]
            counts["created"] += 1
        elif visible and update_existing:
            counts[
                _update_existing_event(calendar_client, calendar_event_id, fields, dry_run=dry_run)
            ] += 1

        occurrence_map[occurrence_id] = {**stored, "calendar_event_id": calendar_event_id}

    summary = " ".join(f"{name}={count}" for name, count in counts.items() if count)
    print(
        f"  Activity {activity_id} {activity.get('Name')!r}: visible={visible} "
        f"occurrences={len(occurrence_map)} {summary}".rstrip()
    )

    if dry_run:
        return

    program_id = record["program_id"]
    if program_id:
        event_store.add_membership("Program", program_id, "Activities", activity_id)

    event_store.set(
        "Activity",
        activity_id,
        {
            **record,
            "occurrences": occurrence_map,
            # Kept, so a webhook older than the last one applied is still
            # recognised as stale after a backfill.
            "last_event_time": (existing or {}).get("last_event_time"),
        },
        custom_time=record["end_date"],
    )


def _update_existing_event(
    calendar_client: CalendarClient, calendar_event_id: str, fields: dict, *, dry_run: bool
) -> str:
    """Patches one existing event if it differs. Returns which count it falls under."""
    event = calendar_client.get_event(calendar_event_id)
    if event is None or event.get("status") == "cancelled":
        print(f"    event {calendar_event_id}: missing or cancelled, left alone")
        return "missing"
    changes = event_changes(event, fields)
    if not changes:
        return "unchanged"
    for name, (have, wanted) in changes.items():
        print(f"    event {calendar_event_id} {name}: {have!r} -> {wanted!r}")
    if not dry_run:
        calendar_client.update_event(calendar_event_id, **{name: fields[name] for name in changes})
    return "updated"


def backfill_facility_booking(
    calendar_client: CalendarClient, event_store: EventStore, reservation: dict, *, dry_run: bool
) -> None:
    reservation_id = reservation["ReservationId"]
    raw_title = reservation.get("Title")

    if reservation.get("Type") == "Activity":
        # A booked session of an Activity, not a standalone facility use —
        # already fully covered by backfill_activity() via the Program ->
        # Activities -> Occurrences path. Importing it here too would
        # create a second, untracked-by-Program-visibility calendar event
        # for the same real-world session.
        print(f"  FacilityBooking {reservation_id} {raw_title!r}: Type=Activity, skipped")
        return

    if reservation.get("IsCancelled"):
        print(f"  FacilityBooking {reservation_id} {raw_title!r}: cancelled, skipped")
        return

    if event_store.get("FacilityBooking", reservation_id) is not None:
        print(f"  FacilityBooking {reservation_id} {raw_title!r}: already tracked, skipped")
        return

    title = clean_title(reservation.get("Title"), "Facility Booking")
    print(f"  FacilityBooking {reservation_id} {title!r}: creating")

    if dry_run:
        return

    event = calendar_client.create_event(
        event_id=stable_event_id("FacilityBooking", reservation_id),
        summary=f"{title} booking",
        start_iso=reservation["Start"],
        end_iso=reservation["End"],
        location=(reservation.get("Location") or {}).get("Name"),
        extended_properties={
            "amilia_type": "FacilityBooking",
            "amilia_type_id": str(reservation_id),
        },
    )
    event_store.set(
        "FacilityBooking",
        reservation_id,
        {"calendar_event_id": event["id"], "action": "Create"},
        custom_time=reservation["End"],
    )


def backfill_facility_bookings(
    calendar_client: CalendarClient,
    event_store: EventStore,
    amilia: AmiliaClient,
    org_id,
    from_date: str,
    to_date: str,
    *,
    enabled: bool,
    dry_run: bool,
) -> list[tuple[str, str]]:
    """
    Backfills every reservation in [from_date, to_date], returning
    (ReservationId, error) for each one that failed. Does nothing, not even
    the /reservations fetch, unless FACILITY_BOOKING_SYNC_ENABLED is on
    (`enabled`), mirroring the live webhook ignoring FacilityBooking then.
    """
    if not enabled:
        print("\nSkipping reservations: FACILITY_BOOKING_SYNC_ENABLED is off.")
        return []

    print(f"\nFetching reservations ({from_date} to {to_date})...")
    reservations = amilia.get_reservations(org_id, from_date, to_date)
    print(f"Found {len(reservations)} reservation(s)")

    failed = []
    for reservation in reservations:
        try:
            backfill_facility_booking(calendar_client, event_store, reservation, dry_run=dry_run)
        except Exception as exc:
            failed.append((reservation["ReservationId"], str(exc)))
            print(f"  FAILED reservation {reservation['ReservationId']}: {exc}", file=sys.stderr)
        time.sleep(REQUEST_PACING_SECONDS)

    print(f"\nDone. {len(reservations)} reservation(s) processed, {len(failed)} failed.")
    return failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-id", required=True, help="Amilia OrganizationId")
    parser.add_argument("--amilia-username", required=True)
    parser.add_argument("--amilia-password", required=True)
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=30,
        help="Skip activities whose last occurrence ended more than this many days ago. Also used "
        "as the start of the FacilityBooking date range.",
    )
    parser.add_argument(
        "--reservation-lookahead-days",
        type=int,
        default=730,
        help="End of the FacilityBooking date range — how far into the future to fetch "
        "reservations (the /reservations endpoint requires an explicit bound; there's no true "
        "'unbounded').",
    )
    parser.add_argument(
        "--update-existing",
        action="store_true",
        help="Also patch existing Activity events whose title, times, location, description or "
        "Amilia tags differ from Amilia's. Never deletes or recreates an event. With --dry-run, "
        "prints each change without writing it.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without writing to the calendar or the store.",
    )
    args = parser.parse_args()

    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    bucket_name = os.environ.get("GOOGLE_EVENT_STORE_BUCKET")
    if not calendar_id or not bucket_name:
        print("GOOGLE_CALENDAR_ID and GOOGLE_EVENT_STORE_BUCKET must both be set", file=sys.stderr)
        return 2

    print(f"Authenticating to GCP as: {_identity()}")
    print(f"Target calendar: {calendar_id}")
    print(f"Target bucket:   {bucket_name}")
    if args.dry_run:
        print("DRY RUN — nothing will actually be written")

    calendar_client = CalendarClient(calendar_id=calendar_id)
    event_store = EventStore(bucket_name=bucket_name)
    amilia = AmiliaClient(
        args.amilia_username, args.amilia_password, pacing_seconds=REQUEST_PACING_SECONDS
    )
    cutoff = datetime.now(UTC) - timedelta(days=args.lookback_days)

    print("\nFetching programs...")
    programs = amilia.get_programs(args.org_id)
    print(f"Found {len(programs)} program(s)")

    program_online_by_id = {}
    for program in programs:
        program_online_by_id[program["Id"]] = backfill_program(
            event_store, program, dry_run=args.dry_run
        )
        time.sleep(REQUEST_PACING_SECONDS)

    total_activities = 0
    skipped_activities = 0
    failed = []

    for program in programs:
        print(f"\nFetching activities for program {program['Id']} {program.get('Name')!r}...")
        activities = amilia.get_program_activities(args.org_id, program["Id"])
        for activity in activities:
            if not is_within_lookback(activity, cutoff):
                skipped_activities += 1
                continue
            total_activities += 1
            try:
                backfill_activity(
                    calendar_client,
                    event_store,
                    amilia,
                    args.org_id,
                    activity,
                    program_online=program_online_by_id.get(program["Id"], True),
                    dry_run=args.dry_run,
                    update_existing=args.update_existing,
                )
            except Exception as exc:
                failed.append((activity["Id"], str(exc)))
                print(f"  FAILED activity {activity['Id']}: {exc}", file=sys.stderr)
            time.sleep(REQUEST_PACING_SECONDS)

    print(
        f"\nDone. {len(programs)} program(s), {total_activities} activity(ies) processed, "
        f"{skipped_activities} skipped (ended more than {args.lookback_days} days ago), "
        f"{len(failed)} failed."
    )

    from_date = cutoff.strftime("%Y-%m-%d")
    to_date = (datetime.now(UTC) + timedelta(days=args.reservation_lookahead_days)).strftime(
        "%Y-%m-%d"
    )
    failed.extend(
        backfill_facility_bookings(
            calendar_client,
            event_store,
            amilia,
            args.org_id,
            from_date,
            to_date,
            enabled=facility_booking_sync_enabled(),
            dry_run=args.dry_run,
        )
    )
    if failed:
        for external_id, error in failed:
            print(f"  {external_id}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
