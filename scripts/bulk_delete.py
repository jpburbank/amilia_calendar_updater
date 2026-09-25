"""
Bulk deletion tool: removes event-store documents (and, optionally, the
Google Calendar events they reference) for a chosen context. Built for two
very different situations that share the same dangerous shape — clearing
out repeated test data, and emergency cleanup against production — so it
defaults to the more paranoid behavior throughout rather than assuming
which one you're in.

Deliberately standalone, same reasoning as scripts/backfill.py: makes no
assumptions about src/webhook/, reuses only src/shared/ (event_store.py,
calendar_client.py).

Safety model (all of these apply every time, test or prod alike — there is
no "test bucket" heuristic to get wrong):
  - Nothing is deleted unless --execute is passed. Without it, this only
    resolves and prints what *would* be deleted.
  - Even with --execute, you must also pass --confirm-bucket <name> with the
    exact GOOGLE_EVENT_STORE_BUCKET value. This is a second, independent
    gate that can't be satisfied by a copy-pasted flag alone — it has to be
    read and retyped.
  - A resolved selection larger than --limit (default 25) refuses to run at
    all; raise --limit explicitly if you really mean that many.
  - Every real run appends to a timestamped file under audit_logs/ as it
    goes (not just a summary at the end), so a crash mid-run still leaves a
    record of what was actually deleted before it happened.

Selection (exactly one; see --help): --id (one or more explicit external
IDs), --program-id (every Activity belonging to that Program, via the
event store's membership markers), --older-than-days (every document in
the context whose store `updated_at` is older than N days — generic across
contexts, unlike domain date fields, which differ by context), or --all
(every document in the context).

Run once, impersonating the function's own service account identity, same
as backfill.py:
    gcloud auth application-default login \
      --impersonate-service-account=<SA_EMAIL>

    GOOGLE_CALENDAR_ID=... GOOGLE_EVENT_STORE_BUCKET=... \
      PYTHONPATH=src python scripts/bulk_delete.py \
      --context Activity --program-id 107638

See scripts/BULK_DELETE.md for full usage and examples.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import google.auth
import google.auth.transport.requests

from shared.calendar_client import CalendarClient
from shared.event_store import EventStore

CONTEXTS = ["Program", "Activity", "FacilityBooking"]
AUDIT_LOG_DIR = Path(__file__).resolve().parent.parent / "audit_logs"


def _identity() -> str:
    """Best-effort email for whoever we authenticated to GCP as."""
    credentials, _ = google.auth.default()
    email = getattr(credentials, "service_account_email", None)
    if not email or email == "default":
        credentials.refresh(google.auth.transport.requests.Request())
        email = getattr(credentials, "service_account_email", None)
    return email or "<not a service account — probably your user account>"


def selection_label(args: argparse.Namespace) -> str:
    if args.ids:
        return "ids"
    if args.program_id:
        return f"program-{args.program_id}"
    if args.older_than_days is not None:
        return f"olderthan{args.older_than_days}d"
    return "all"


def resolve_targets(event_store: EventStore, context: str, args: argparse.Namespace) -> list[str]:
    if args.ids:
        return list(dict.fromkeys(args.ids))  # de-duplicate, preserve order
    if args.program_id:
        return event_store.list_members("Program", args.program_id, "Activities")
    if args.older_than_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.older_than_days)
        matches = []
        for external_id in event_store.list_all(context):
            doc = event_store.get(context, external_id)
            updated_at = (doc or {}).get("updated_at")
            if updated_at and datetime.fromisoformat(updated_at) < cutoff:
                matches.append(external_id)
        return matches
    return event_store.list_all(context)


def extract_calendar_event_ids(context: str, doc: dict) -> list[str]:
    """Every calendar_event_id a document references — the shape differs by
    context (Activity: one per occurrence; FacilityBooking: a single field;
    Program: none — Programs have no calendar events of their own)."""
    if context == "Activity":
        occurrences = doc.get("occurrences") or {}
        return [o["calendar_event_id"] for o in occurrences.values() if o.get("calendar_event_id")]
    if context == "FacilityBooking":
        event_id = doc.get("calendar_event_id")
        return [event_id] if event_id else []
    return []


def cleanup_memberships(event_store: EventStore, context: str, external_id: str, doc: dict) -> None:
    """Removes membership markers left dangling by this deletion. Doesn't
    touch the other side's document — only the marker(s) pointing at/from
    the thing being deleted."""
    if context == "Activity" and doc and doc.get("program_id"):
        event_store.remove_membership("Program", doc["program_id"], "Activities", external_id)
    elif context == "Program":
        for activity_id in event_store.list_members("Program", external_id, "Activities"):
            event_store.remove_membership("Program", external_id, "Activities", activity_id)


def delete_one(
    calendar_client: CalendarClient | None,
    event_store: EventStore,
    context: str,
    external_id: str,
    *,
    target: str,
) -> dict:
    """Deletes one document's requested target(s). Returns a result dict
    describing what happened, for logging/reporting — never raises for a
    calendar failure (recorded instead), so one bad event doesn't abort the
    store deletion or the rest of the run."""
    doc = event_store.get(context, external_id) or {}
    result = {"id": external_id, "calendar_deleted": [], "calendar_failed": [], "store_deleted": False}

    if target in ("calendar", "both"):
        for event_id in extract_calendar_event_ids(context, doc):
            try:
                calendar_client.delete_event(event_id)
                result["calendar_deleted"].append(event_id)
            except Exception as exc:
                result["calendar_failed"].append((event_id, str(exc)))

    if target in ("store", "both"):
        cleanup_memberships(event_store, context, external_id, doc)
        event_store.delete(context, external_id)
        result["store_deleted"] = True

    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--context", required=True, choices=CONTEXTS)

    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--id", dest="ids", action="append", metavar="EXTERNAL_ID", help="May be repeated.")
    selection.add_argument("--program-id", help="Every Activity belonging to this Program (--context Activity only).")
    selection.add_argument("--older-than-days", type=int, help="Every document whose store record is older than this.")
    selection.add_argument("--all", action="store_true", help="Every document in --context. Still subject to --limit.")

    parser.add_argument("--target", choices=["store", "calendar", "both"], default="both")
    parser.add_argument("--limit", type=int, default=25, help="Refuse to run if the resolved selection exceeds this.")
    parser.add_argument("--execute", action="store_true", help="Actually delete. Without this, preview only.")
    parser.add_argument("--confirm-bucket", metavar="BUCKET_NAME", help="Required with --execute: must exactly match GOOGLE_EVENT_STORE_BUCKET.")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()

    if args.program_id and args.context != "Activity":
        print("--program-id only makes sense with --context Activity", file=sys.stderr)
        return 2

    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    bucket_name = os.environ.get("GOOGLE_EVENT_STORE_BUCKET")
    if not calendar_id or not bucket_name:
        print("GOOGLE_CALENDAR_ID and GOOGLE_EVENT_STORE_BUCKET must both be set", file=sys.stderr)
        return 2

    if args.execute and args.confirm_bucket != bucket_name:
        print(
            f"Refusing to run: --confirm-bucket must exactly match GOOGLE_EVENT_STORE_BUCKET "
            f"({bucket_name!r}), got {args.confirm_bucket!r}.",
            file=sys.stderr,
        )
        return 2

    run_started_at = datetime.now(timezone.utc)

    print(f"Authenticating to GCP as: {_identity()}")
    print(f"Target bucket:   {bucket_name}")
    print(f"Target calendar: {calendar_id}")
    print(f"Context: {args.context}   Target: {args.target}   Selection: {selection_label(args)}")

    event_store = EventStore(bucket_name=bucket_name)
    calendar_client = CalendarClient(calendar_id=calendar_id) if args.target in ("calendar", "both") else None

    targets = resolve_targets(event_store, args.context, args)
    print(f"\nResolved {len(targets)} document(s):")
    for external_id in targets:
        print(f"  {args.context}/{external_id}")

    if not targets:
        print("\nNothing matches this selection.")
        return 0

    if len(targets) > args.limit:
        print(
            f"\nRefusing to run: {len(targets)} document(s) exceed --limit ({args.limit}). "
            "Narrow the selection or pass a higher --limit if this is really intended.",
            file=sys.stderr,
        )
        return 2

    if not args.execute:
        print("\nDRY RUN — nothing deleted. Re-run with --execute (and --confirm-bucket) to delete.")
        return 0

    AUDIT_LOG_DIR.mkdir(exist_ok=True)
    timestamp = run_started_at.strftime("%Y%m%dT%H%M%SZ")
    log_path = AUDIT_LOG_DIR / f"{args.context}_{selection_label(args)}_{args.target}_{timestamp}.log"

    failed = []
    with open(log_path, "w") as log:
        log.write(f"started_at={run_started_at.isoformat()}\n")
        log.write(f"identity={_identity()}\n")
        log.write(f"bucket={bucket_name}\n")
        log.write(f"calendar={calendar_id}\n")
        log.write(f"context={args.context}\ntarget={args.target}\nselection={selection_label(args)}\n")
        log.write(f"resolved_count={len(targets)}\n---\n")
        log.flush()

        print(f"\nAudit log: {log_path}")
        print("Deleting...")
        for external_id in targets:
            result = delete_one(calendar_client, event_store, args.context, external_id, target=args.target)
            log.write(f"{external_id}: {result}\n")
            log.flush()
            print(f"  {external_id}: {result}")
            if result["calendar_failed"]:
                failed.append((external_id, result["calendar_failed"]))

        summary = f"\nDone. {len(targets)} document(s) processed, {len(failed)} with calendar failures.\n"
        log.write(summary)
        print(summary)

    if failed:
        for external_id, calendar_failed in failed:
            print(f"  {external_id}: {calendar_failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
