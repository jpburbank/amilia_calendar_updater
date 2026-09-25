# `scripts/bulk_delete.py`

Deletes event-store documents (`Program` / `Activity` / `FacilityBooking`), and
optionally the Google Calendar events they reference, in bulk. Used for two
different situations:

- **Repeated testing** — clearing out data created by prior test runs
  (backfill re-runs, manual webhook testing, etc.) so the next run starts
  clean.
- **Emergency cleanup** — a real incident (e.g. an Amilia webhook storm, or a
  bad Program change) that needs deleted from production directly. This is
  the reason the tool is more paranoid than a typical test-cleanup script.

Deliberately standalone — see the module docstring in `bulk_delete.py` for
why it doesn't import from `src/webhook/`.

## What `--context` means

`--context` picks which *kind* of thing you're deleting. It's not a free-text
value — it's always one of `Program`, `Activity`, or `FacilityBooking`, the
same three object types Amilia sends webhooks for and this project mirrors in
its event store (see `src/shared/event_store.py`). Every document in the
store is filed under exactly one of these, so `--context` scopes the whole
run to one type at a time — you can't mix, say, deleting a Program and an
Activity in the same invocation.

| `--context` | What it is in Amilia | What deleting it removes |
|---|---|---|
| `Program` | A container for Activities (e.g. a season of classes). Has no calendar event of its own. | Just the store record of whether it's online/offline. `--target calendar` is a no-op here. |
| `Activity` | A specific class/offering within a Program, with one or more scheduled occurrences. | The store record (including all tracked occurrences) and every Google Calendar event created for those occurrences. |
| `FacilityBooking` | A single booking of a space/room. | The store record and the one Google Calendar event it created. |

Concretely, `--context Activity --id 7311901` means "the Activity document
whose Amilia ID is 7311901" — nothing about Programs or FacilityBookings is
touched, even if IDs happen to collide across contexts (which is also why the
store namespaces documents by context in the first place — see the
`Object naming` note in `event_store.py`).

## Running it

Impersonate the function's own service account, same as `scripts/backfill.py`:

```
gcloud auth application-default login \
  --impersonate-service-account=amilia-calendar-updater@cm-calendar-506017.iam.gserviceaccount.com

GOOGLE_CALENDAR_ID=... GOOGLE_EVENT_STORE_BUCKET=... \
  PYTHONPATH=src python scripts/bulk_delete.py \
  --context Activity --program-id 107638
```

Every invocation requires `--context` and **exactly one** selection flag:

| Flag | Selects |
|---|---|
| `--id EXTERNAL_ID` (repeatable) | Specific document(s) by ID |
| `--program-id ID` | Every `Activity` belonging to that `Program` (via the event store's membership markers — `--context Activity` only) |
| `--older-than-days N` | Every document in `--context` whose store `updated_at` is older than N days |
| `--all` | Every document in `--context` |

Optional:

- `--target {store,calendar,both}` (default `both`) — `store` only touches
  the event-store document; `calendar` only deletes the Google Calendar
  event(s) it references; `both` does both. Useful when store and calendar
  have already drifted out of sync and you only want to fix one side.
- `--limit N` (default `25`) — the run refuses to proceed if the resolved
  selection is larger than this. Raise it explicitly if you really mean to
  delete that many; this exists so a mistyped `--older-than-days` or
  `--all` on the wrong context can't silently take out everything.

## What actually happens

**Without `--execute`, nothing is deleted.** The script resolves the
selection, prints every document it matched, and stops — a dry-run preview by
default (the opposite default from `backfill.py`, since this tool deletes
rather than creates).

To actually delete, you need **both**:

```
--execute --confirm-bucket <exact GOOGLE_EVENT_STORE_BUCKET value>
```

`--confirm-bucket` has to match the bucket name exactly, or the run is
refused. This is deliberately not weakened by any "looks like a test bucket"
heuristic — it applies the same way whether you're pointed at a test
environment or production, so there's one thing to get right instead of a
rule that might be wrong. Because it has to be typed/pasted correctly rather
than just present, a `--execute` flag left over in shell history can't delete
anything by itself.

Deleting a document also cleans up the membership markers that reference it
(e.g. deleting a `Program` removes its `Program/{id}/Activities/*` markers;
deleting an `Activity` removes its own marker under its parent `Program`), so
repeated runs don't accumulate orphaned markers.

## Audit trail

Every real (`--execute`) run writes a log to `audit_logs/` (gitignored — these
are local operational records, not project history) as deletions happen, not
just a summary at the end, so a crash mid-run still leaves a record of what
was actually deleted before that point.

Filename: `{context}_{selection}_{target}_{timestamp}.log`, timestamped at
the *start* of the run — e.g.:

```
audit_logs/Activity_program-107638_both_20260925T192530Z.log
audit_logs/FacilityBooking_olderthan30d_store_20260926T081145Z.log
```

Each file records the identity that ran it, the target bucket/calendar, the
selection criteria, and a per-document line as each delete completes
(including any calendar-delete failures, which don't abort the run — a
single bad event doesn't block deleting the rest of the selection).

Dry-run previews (no `--execute`) don't write a log — nothing happened yet to
audit.

## Exit codes

- `0` — success (including a dry-run preview, or a selection that matched
  nothing).
- `1` — ran, but at least one calendar delete failed (see the log for which).
- `2` — refused to run at all (bad flags, missing env vars, `--confirm-bucket`
  mismatch, or the selection exceeded `--limit`).
