# Bulk delete

`scripts/bulk_delete.py` deletes event-store records, and optionally the calendar events they
point to, in bulk. Use it to clear test data or for emergency cleanup.

It **never deletes anything unless you ask twice**: once with `--execute`, and again by typing
the bucket name with `--confirm-bucket`. Without both, it only shows what it would delete.


## Before you start

1. **Act as the service account.** The script uses your machine's default Google login, so point
   that at the service account (`$SA_EMAIL`: see [Deployment variables](operations.md#deployment-variables)):
   ```
   gcloud auth application-default login --impersonate-service-account=$SA_EMAIL
   ```
2. **Set the calendar and bucket** for this shell:
   ```
   export GOOGLE_CALENDAR_ID=$(gcloud secrets versions access latest --secret=google-calendar-id)
   export GOOGLE_EVENT_STORE_BUCKET=$(cd ../amilia_calendar_updater_gcp_resources && terraform output -raw amilia_calendar_event_mappings_bucket)
   ```
3. **Find the IDs** you want to delete:
   - **Activity:** the number at the end of its Amilia URL, e.g. `…/shop/activities/7493601`. It's
     also in each calendar event's hidden `amilia_type_id` tag.
   - **Program:** the same, from the Program's Amilia URL. Classes and Workshops is `107638`.


## Examples

Every command below is a **preview**. Check the list it prints, then run it again with
`--execute --confirm-bucket $GOOGLE_EVENT_STORE_BUCKET` added.

All commands start with:

```
PYTHONPATH=src .venv/bin/python scripts/bulk_delete.py
```

| To delete… | Add |
|---|---|
| One Activity's events and record | `--context Activity --id 7493601` |
| Several Activities | `--context Activity --id 7493601 --id 7493610` |
| Only an Activity's record, keeping its calendar events | `--context Activity --id 7493601 --target store` |
| Only an Activity's calendar events, keeping its record | `--context Activity --id 7493601 --target calendar` |
| Every Activity in a Program | `--context Activity --program-id <Program Id>` |
| Records not updated for 30 days | `--context Activity --older-than-days 30` |
| Every Activity | `--context Activity --all --limit 1000` |

A real delete of one Activity looks like this:

```
PYTHONPATH=src .venv/bin/python scripts/bulk_delete.py \
  --context Activity --id 7493601 \
  --execute --confirm-bucket $GOOGLE_EVENT_STORE_BUCKET
```

**Take care with `--program-id 107638`.** Almost every real class is in Classes and Workshops, so
that selects nearly all of them. `--limit` stops it unless you raise the limit.


## Options

| Option | Meaning |
|---|---|
| `--context` | Required. `Activity`, `Program` or `FacilityBooking` |
| `--id` / `--program-id` / `--older-than-days` / `--all` | Pick exactly one way to select. `--program-id` works only with `--context Activity` |
| `--target` | `both` (default), `store` (records only) or `calendar` (events only) |
| `--limit` | Refuses to run if more than this many records match. Default 25 |
| `--execute` + `--confirm-bucket` | Actually delete. The bucket name must match exactly |

What each `--context` removes:

- **Activity:** its record and every calendar event for its occurrences.
- **Program:** its record only. Programs have no calendar events, so `--target calendar` does
  nothing.
- **FacilityBooking:** its record and its one calendar event.

Deleting a record also removes its links to its Program, so later `--program-id` runs don't
find it.


## After you delete

- **If the Activity still exists in Amilia, it's no longer synced.** Its next Update webhook is
  skipped (`no stored activity`), so its events don't come back by themselves. Run the
  [backfill](operations.md#backfill) to bring it back.
- **There's no undo command, but the backfill restores events.** Events are recreated under the
  same IDs, so a deleted event comes back rather than being duplicated.
- **Every real run writes an audit log** to `audit_logs/` (not committed to git), one line per
  record as it goes. It records who ran it, the bucket, the calendar and what was selected, and
  survives a crash mid-run. Previews don't write one.
- **Each record's result** lists `calendar_deleted`, `calendar_already_gone` (already removed from
  the calendar, which is fine) and `calendar_failed`. A calendar failure doesn't stop the run.


## Exit codes

| Code | Meaning |
|---|---|
| `0` | Done, or a preview, or nothing matched |
| `1` | Done, but some calendar events couldn't be deleted. The output and audit log name them |
| `2` | Refused to run: bad options, missing settings, wrong `--confirm-bucket`, or over `--limit` |
