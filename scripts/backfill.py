"""
One-time bulk loader: seeds the event store (and creates matching calendar
events) for Programs and Activities that already existed in Amilia before
this integration's webhook subscriptions did. Webhooks only deliver
changes going forward — without this, anything created before those
subscriptions existed would only be discovered by the fail-open "Program
unknown -> assumed visible" path (for Programs), or never discovered at
all (for Activities, which have no equivalent fallback).

Deliberately standalone: does not import anything from src/webhook/ (that
package is specifically the Cloud Function's own code, including a Cloud
Tasks dependency this script has no use for) — it makes its own Amilia
REST calls and writes calendar/store state directly, reusing only
src/shared/ (calendar_client.py, event_store.py), which is designed to be
shared by exactly this kind of caller. The Amilia REST-calling logic below
duplicates part of src/webhook/amilia_client.py rather than importing it,
by design.

Run once, impersonating the function's own service account identity (the
same identity real traffic runs as, so this write exercises the same IAM
path production does — see scripts/check_access.py for why):
    gcloud auth application-default login \
      --impersonate-service-account=<SA_EMAIL>

    GOOGLE_CALENDAR_ID=... GOOGLE_EVENT_STORE_BUCKET=... \
      PYTHONPATH=src python scripts/backfill.py \
      --org-id 17659 --amilia-username <user> --amilia-password <pass>

Add --dry-run first to preview what would happen without writing anything.

Safe to re-run: an occurrence that already has a stored calendar_event_id
is left alone rather than duplicated.

Scope: every Program (there are only ever a handful). Every Activity whose
EndDate is not more than --lookback-days in the past (default 30) — no
upper/forward bound, so anything upcoming is always included regardless of
how far out it starts. An Activity that ended further back than that is
treated as no longer relevant and skipped.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import google.auth
import google.auth.transport.requests
import requests

from shared.calendar_client import CalendarClient
from shared.event_store import EventStore

AUTHENTICATE_URL = "https://www.amilia.com/api/V3/authenticate"
API_BASE_URL = "https://app.amilia.com/api/v3/en"

REQUEST_PACING_SECONDS = 0.2  # be gentle on Amilia's API


def _identity() -> str:
    """Best-effort email for whoever we authenticated to GCP as."""
    credentials, _ = google.auth.default()
    email = getattr(credentials, "service_account_email", None)
    if not email or email == "default":
        credentials.refresh(google.auth.transport.requests.Request())
        email = getattr(credentials, "service_account_email", None)
    return email or "<not a service account — probably your user account>"


class _AmiliaRestClient:
    """
    Minimal, backfill-only Amilia REST client — deliberately not shared
    with src/webhook/amilia_client.py, per this script's module docstring.
    """

    def __init__(self, username: str, password: str):
        self._username = username
        self._password = password
        self._session = requests.Session()
        self._token: str | None = None

    def get_programs(self, org_id) -> list[dict]:
        return self._get_all_pages(f"{API_BASE_URL}/org/{org_id}/programs")

    def get_program_activities(self, org_id, program_id) -> list[dict]:
        return self._get_all_pages(f"{API_BASE_URL}/org/{org_id}/programs/{program_id}/activities")

    def get_activity_occurrences(self, org_id, activity_id) -> list[dict]:
        return self._get_all_pages(
            f"{API_BASE_URL}/org/{org_id}/activities/{activity_id}/occurrences"
        )

    def _get_all_pages(self, url: str) -> list[dict]:
        items = []
        page = 1
        while True:
            data = self._get(url, params={"page": page, "perPage": 2000}).json()
            items.extend(data["Items"])
            if len(items) >= data["Paging"]["TotalCount"]:
                return items
            page += 1
            time.sleep(REQUEST_PACING_SECONDS)

    def _get(self, url: str, **kwargs) -> requests.Response:
        response = self._session.get(url, headers=self._auth_header(), **kwargs)
        if response.status_code == 401:
            self._token = None
            response = self._session.get(url, headers=self._auth_header(), **kwargs)
        response.raise_for_status()
        return response

    def _auth_header(self) -> dict:
        return {"Authorization": f"Bearer {self._token or self._authenticate()}"}

    def _authenticate(self) -> str:
        response = self._session.get(AUTHENTICATE_URL, auth=(self._username, self._password))
        response.raise_for_status()
        self._token = response.json()["Token"]
        return self._token


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
                "name": program.get("Name"),
                "online": online,
                "start_date": program.get("Start"),
                "expiration_date": program.get("End"),
            },
        )
    return online


def backfill_activity(
    calendar_client: CalendarClient,
    event_store: EventStore,
    amilia: _AmiliaRestClient,
    org_id,
    activity: dict,
    *,
    program_online: bool,
    dry_run: bool,
) -> None:
    activity_id = activity["Id"]
    status = activity.get("Status")
    visible = program_online and status == "Normal"

    existing = event_store.get("Activity", activity_id)
    existing_occurrences = (existing or {}).get("occurrences") or {}

    occurrences = amilia.get_activity_occurrences(org_id, activity_id)
    location = activity.get("LocationLabel") or None
    summary = activity.get("Name", "Activity")

    occurrence_map = {}
    created = 0
    for occurrence in occurrences:
        occurrence_id = str(occurrence["Id"])
        previous = existing_occurrences.get(occurrence_id) or {}
        calendar_event_id = previous.get("calendar_event_id")

        if visible and not calendar_event_id:
            if not dry_run:
                event = calendar_client.create_event(
                    summary=summary,
                    start_iso=occurrence["Start"],
                    end_iso=occurrence["End"],
                    location=location,
                )
                calendar_event_id = event["id"]
            created += 1

        occurrence_map[occurrence_id] = {
            "start": occurrence["Start"],
            "end": occurrence["End"],
            "calendar_event_id": calendar_event_id,
        }

    print(
        f"  Activity {activity_id} {activity.get('Name')!r}: visible={visible} "
        f"occurrences={len(occurrence_map)} created={created}"
    )

    if dry_run:
        return

    program_id = activity.get("ProgramId") or None
    if program_id:
        event_store.add_membership("Program", program_id, "Activities", activity_id)

    event_store.set(
        "Activity",
        activity_id,
        {
            "name": activity.get("Name"),
            "program_id": program_id,
            "category_id": activity.get("CategoryId") or None,
            "category_name": activity.get("CategoryName"),
            "sub_category_id": activity.get("SubCategoryId") or None,
            "sub_category_name": activity.get("SubCategoryName"),
            "status": status,
            "start_date": activity.get("StartDate"),
            "end_date": activity.get("EndDate"),
            "location": location,
            "occurrences": occurrence_map,
        },
        custom_time=activity.get("EndDate"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-id", required=True, help="Amilia OrganizationId")
    parser.add_argument("--amilia-username", required=True)
    parser.add_argument("--amilia-password", required=True)
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=30,
        help="Skip activities whose last occurrence ended more than this many days ago.",
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
    amilia = _AmiliaRestClient(args.amilia_username, args.amilia_password)
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.lookback_days)

    print("\nFetching programs...")
    programs = amilia.get_programs(args.org_id)
    print(f"Found {len(programs)} program(s)")

    program_online_by_id = {}
    for program in programs:
        program_online_by_id[program["Id"]] = backfill_program(event_store, program, dry_run=args.dry_run)
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
    if failed:
        for activity_id, error in failed:
            print(f"  {activity_id}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
