# Amilia Calendar Updater

Keeps a Google Calendar in sync with Create Makerspace's classes in Amilia. When an Activity is
created, changed or deleted in Amilia, its calendar events follow within seconds.

Open security issues are tracked in [`docs/security-notes.md`](docs/security-notes.md).


## How it works

```
Amilia webhook ──► amilia-calendar-updater ──► queue ──► amilia-webhook-worker ──► Google Calendar
                   (public: checks token,                (private: does the sync)    + event store
                    queues, replies in ms)                       │
                                                                 └─► amilia-activity-reconciler
                                                                     (when a Program goes online/offline)
```

- **Three Cloud Functions.** `amilia-calendar-updater` receives Amilia's webhooks,
  `amilia-webhook-worker` processes them, and `amilia-activity-reconciler` shows or hides a
  Program's Activities when the Program's `Online` flag changes.
- **Amilia's REST API** fills in what webhooks don't carry: each occurrence's times and room, and
  each record's current state.
- **The event store** (a Cloud Storage bucket) remembers which calendar event belongs to which
  Amilia occurrence.


## What gets synced

Amilia **Programs**, **Activities**, and each Activity's **occurrences**. Each occurrence (session)
is one calendar event.

**FacilityBookings are off by default.** The calendar no longer needs them (since 2026-11), but
the code is kept and tested behind a flag in `env.yaml`:

| `FACILITY_BOOKING_SYNC_ENABLED` | Effect |
|---|---|
| `"false"` (default) | FacilityBooking webhooks are answered `status=ignored`; the backfill skips reservations |
| `"true"` | FacilityBooking webhooks and reservations are synced |

Any other value stops the function at startup. A change takes effect on the next deploy. Turning
it off leaves existing FacilityBooking events and records in place, no longer updated. See
`src/shared/feature_flags.py`.


## What goes into a calendar event

| Event field | Comes from |
|---|---|
| Title | The Activity's `Name`, trimmed of stray spaces (`Activity` if blank) |
| Start / end | The occurrence's `Start` / `End` from Amilia's REST API |
| Location | The occurrence's room, then the building address: `Wood shop, 39449 Queens Way #1, Squamish, BC V8B 0R5` |
| Colour | Picked from the room (e.g. Pottery Studio → Tangerine); an unlisted room gets the default |
| Description | A **Book Now** link, then Description, Note, Prerequisite, Additional Information, Contact person |
| Hidden tags | `amilia_type` and `amilia_type_id`, identifying the Amilia record |

**To change the building address or the room colours, edit `src/shared/constants.py`.**

- **Room.** The occurrence's `Location.Name`, falling back to the Activity's
  `Schedules[].Locations[].Name`. Only the room is stored; the address is added when the event is
  written. See `src/shared/locations.py`.
- **Book Now.** Links to the Activity's `Url`, or its `SecretUrl` if `Url` isn't a valid http(s)
  URL. No link if neither is valid.
- **Description sections.** Each is its own paragraph, with a bold heading except Description
  itself. Empty ones are left out.
- **Hidden tags.** Stored in the event's `extendedProperties.private`. They never show in the
  Calendar UI, but anyone with API read access to the event can read them.

The webhook worker, the reconciler and the backfill all build events with the same code
(`src/shared/activities.py`), so they always agree.

**Existing events** pick up changes to any of the above on their Activity's next Update, or when
the backfill runs with `--update-existing`.


## How sync stays correct

**Stale webhooks are skipped.** Amilia can redeliver an old webhook minutes later, after newer
ones. Each stored record keeps the `EventTime` of the webhook that last wrote it; a webhook that
isn't newer is answered `status=skipped reason=stale event` and changes nothing. Records without
one (e.g. written by the backfill) accept any webhook.

**Amilia is asked for the current state.** Activity and Program Create/Update webhooks are
checked against Amilia's REST API:

| Amilia says | Result |
|---|---|
| Not found (deleted) | `status=skipped reason=deleted in Amilia` |
| Program archived | `status=skipped reason=archived in Amilia` |
| Activity's current `Status` | Used instead of the webhook's (archived Activities show as `Hidden`) |
| Lookup fails (timeout, 5xx) | Retried; on the final attempt the webhook's own data is used, so an outage doesn't lose changes |

**Activity dates come from REST too.** The webhook's `StartDate`/`EndDate` have the wrong UTC
offset, so stored dates use the REST Activity's. Event times were never affected.

**Retries never duplicate events.** Each event's ID is derived from its Amilia IDs
(`stable_event_id` in `src/shared/calendar_client.py`). A repeated create updates the existing
event instead, restoring it if it was deleted.


## Background webhook processing

`amilia-calendar-updater` checks the token, puts the webhook on the `amilia-webhook-processing`
queue and replies `status=queued` in milliseconds; slow replies (2.7–5.9 s, seen 2026-11-13) made
Amilia redeliver. `amilia-webhook-worker`, called only by Cloud Tasks, does the actual sync.

| Worker result | What happens next |
|---|---|
| `ok` / `skipped` / `ignored` | Done |
| `retrying`: a temporary failure (network, timeout, 429/5xx, rate limit) | Retried with backoff from 10 s, doubling, up to 10 min apart |
| `discarded`: a permanent failure, or 24 h of retries | Given up. **One ERROR log entry holds the full webhook, the error and the traceback**, so the change can be redone by hand |

- **One task at a time.** The queue runs one task at a time, so two changes to the same record
  can't race. Volume is a few dozen webhooks a day.
- **Timing.** The worker gives up after `WEBHOOK_TASK_GIVE_UP_AFTER_SECONDS` (24 h). The queue's
  own limit (Terraform `webhook_task_max_retry_duration`, 25 h) is a backstop and must stay longer.
- **If queueing fails**, the webhook is processed inline instead (`status=ok` or `dropped`).
- **Retry rules** are in `src/webhook/retry_policy.py`.

Find given-up webhooks in Cloud Logging with `jsonPayload.message:"status=discarded"` (or
`textPayload:`). Emailing them is [issue #26](https://github.com/jpburbank/amilia_calendar_updater/issues/26).


## Setup

### Deployment variables

Commands in this README use shell variables, never real project values. Set them once per shell:

```
cd ../amilia_calendar_updater_gcp_resources
export REGION=$(terraform output -raw function_region)
export SA_EMAIL=$(terraform output -raw amilia_calendar_updater_email)
cd -
export AMILIA_ORG_ID=<Amilia OrganizationId>
```

The functions read their settings from `env.yaml` (copy `env.yaml.example`) and their secrets from
Secret Manager. Nothing environment-specific is hardcoded.

### Cloud resources (Terraform)

The bucket, queues, secrets and permissions are created by the sibling
`amilia_calendar_updater_gcp_resources` project. Run `terraform apply` there before deploying.

| Resource | Terraform file | Notes |
|---|---|---|
| Event store bucket | `storage.tf` | Records expire after `event_mapping_retention_days` (default 90). Output `amilia_calendar_event_mappings_bucket` → `GOOGLE_EVENT_STORE_BUCKET` |
| Webhook token | `secrets.tf` | A random value generated by Terraform |
| Calendar ID, Amilia login | `secrets.tf` | Empty secrets; set the values by hand (below) |
| Reconciler queue | `tasks.tf` | Output `reconcile_queue_id` → `RECONCILE_QUEUE_ID` |
| Webhook queue | `tasks.tf` | See [First rollout order](#first-rollout-order) |

**The reconciler must be deployed once before Terraform can grant the queue permission to call
it.** Deploy it, then run `terraform apply` again.

### Webhook token

Amilia can't sign webhooks, so each request must carry a shared secret token. Register each
Amilia webhook at the function's URL with the token appended (`?token=<value>`). Get the value
with:

```
gcloud secrets versions access latest --secret=amilia-webhook-token
```

### Amilia API login

The functions call Amilia's REST API as a dedicated Amilia user, not a staff member's login.
After creating that user in Amilia:

```
printf '%s' '<username>' | gcloud secrets versions add amilia-api-username --data-file=-
printf '%s' '<password>' | gcloud secrets versions add amilia-api-password --data-file=-
```

### Target calendar

The calendar is the `google-calendar-id` secret, so switching between dev and production needs no
code change:

1. Share the new calendar with the service account (**Make changes to events**). Check it with
   `scripts/check_access.py`.
2. Store the new ID:
   ```
   printf '%s' '<calendar-id>@group.calendar.google.com' | \
     gcloud secrets versions add google-calendar-id --data-file=-
   ```
3. Redeploy **all three** functions. Secrets are read when an instance starts, so redeploying is
   what makes every instance switch.
4. **Reset the event store.** Its records point at events in the *old* calendar. Clear them by
   hand, then run the backfill against the new calendar.


## Deploying

### CLI

From the project root. Always run `scripts/build.sh` first (see [Why build.sh](#why-buildsh)).

```
./scripts/build.sh

SECRETS="GOOGLE_CALENDAR_ID=google-calendar-id:latest,AMILIA_WEBHOOK_TOKEN=amilia-webhook-token:latest,AMILIA_API_USERNAME=amilia-api-username:latest,AMILIA_API_PASSWORD=amilia-api-password:latest"

gcloud functions deploy amilia-calendar-updater --gen2 --runtime=python312 --region=$REGION \
    --source=src/webhook --entry-point=amilia_webhook --trigger-http --allow-unauthenticated \
    --service-account=$SA_EMAIL --env-vars-file=env.yaml --set-secrets="$SECRETS"

gcloud functions deploy amilia-webhook-worker --gen2 --runtime=python312 --region=$REGION \
    --source=src/webhook --entry-point=process_webhook_task --trigger-http --no-allow-unauthenticated \
    --service-account=$SA_EMAIL --env-vars-file=env.yaml --set-secrets="$SECRETS"

gcloud functions deploy amilia-activity-reconciler --gen2 --runtime=python312 --region=$REGION \
    --source=src/reconciler --entry-point=reconcile_activity --trigger-http --no-allow-unauthenticated \
    --service-account=$SA_EMAIL --env-vars-file=env.yaml \
    --set-secrets="GOOGLE_CALENDAR_ID=google-calendar-id:latest"
```

- **A change under `src/webhook/`** goes to both `amilia-calendar-updater` and
  `amilia-webhook-worker`; they share that code. **A change under `src/shared/`** goes to all
  three.
- **The worker and reconciler are private** (`--no-allow-unauthenticated`): only Cloud Tasks can
  call them.
- **The reconciler is smaller.** It has its own `src/reconciler/requirements.txt`, without the
  Cloud Tasks client or `requests`, and needs only the calendar secret.

### Why build.sh

A deploy uploads only its `--source` directory, but both source directories import `shared/`,
which lives beside them in `src/shared/`. `scripts/build.sh` copies it into `src/webhook/shared/`
and `src/reconciler/shared/` (both gitignored).

Skip it and the deploy either fails with `ModuleNotFoundError: No module named 'shared'`, or,
worse, silently ships an old copy. A symlink isn't used because symlinks don't reliably survive
Google's source upload.

### First rollout order

Only for a fresh environment: the worker's permission grant needs the worker to exist first.

1. In `amilia_calendar_updater_gcp_resources`:
   `terraform apply -target=google_cloud_tasks_queue.webhook_processing -target=google_cloud_tasks_queue_iam_member.amilia_calendar_updater_webhook_enqueuer`
2. Add `WEBHOOK_QUEUE_ID`, `WEBHOOK_WORKER_URL` and `WEBHOOK_TASK_GIVE_UP_AFTER_SECONDS` to
   `env.yaml` (see `env.yaml.example`).
3. Deploy `amilia-webhook-worker`.
4. Run a full `terraform apply` (creates the permission grant).
5. Deploy `amilia-calendar-updater`. Webhooks are queued from this point.


## Scripts

### Backfill

Webhooks only report *changes*, so anything already in Amilia needs loading once.
`scripts/backfill.py` creates the missing events and records for:

- every Program;
- every Activity whose last occurrence ended no more than `--lookback-days` ago (default 30),
  including everything upcoming;
- FacilityBookings, only when `FACILITY_BOOKING_SYNC_ENABLED` is `"true"`.

It's safe to re-run: it never duplicates events, and it keeps each record's `EventTime` so stale
webhooks are still caught. Run it as the service account:

```
gcloud auth application-default login --impersonate-service-account=$SA_EMAIL

GOOGLE_CALENDAR_ID=$(gcloud secrets versions access latest --secret=google-calendar-id) \
GOOGLE_EVENT_STORE_BUCKET=... PYTHONPATH=src .venv/bin/python scripts/backfill.py \
  --org-id $AMILIA_ORG_ID --dry-run
```

Drop `--dry-run` once the preview looks right. Each run ends with a summary per Activity
(`created=` / `updated=` / `unchanged=` / `missing=`).

- **Amilia login.** Read from the same secrets the functions use, in gcloud's configured project
  (`--gcp-project` to override). `--amilia-username` / `--amilia-password` override the secrets.
- **Updating existing events.** Add `--update-existing` to also fix existing Activity events whose
  title, times, location, colour, description or tags differ from Amilia. Only the differing
  fields change, and it never deletes or recreates an event. Hand edits to those fields are
  overwritten, so **preview with `--dry-run` first**. It prints each change, e.g.
  `event 85hn… location: None -> 'Wood shop'`.
- **Reservations.** `/reservations` needs an end date, so FacilityBookings are fetched up to
  `--reservation-lookahead-days` ahead (default 730). Cancelled reservations are skipped, and so
  are those of `Type` `Activity`: they're booked sessions the Activity backfill already covers
  (importing them once created 251 duplicate events).

### Bulk delete

`scripts/bulk_delete.py` deletes records, and optionally their calendar events, by ID, by
Program, by age, or all of one kind. It's for clearing test data and emergency cleanup, so it's
deliberately cautious:

- dry-run by default;
- needs `--execute` plus a `--confirm-bucket` that matches exactly;
- stops past `--limit` documents;
- writes an audit log to `audit_logs/`.

See [`scripts/BULK_DELETE.md`](scripts/BULK_DELETE.md).


## Development

### Python environment

Scripts and tests need this repo's `.venv`, built from Python 3.12 to match the deployed runtime
(`.venv/bin/python -V` to check). A bare `python` fails with `No module named 'google'`.

```
brew install python@3.12
rm -rf .venv
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

### Lint, format and test

Run before every commit ([Ruff](https://docs.astral.sh/ruff/) settings are in `pyproject.toml`):

```
.venv/bin/ruff format .
.venv/bin/ruff check .        # --fix applies safe fixes
.venv/bin/python -m pytest -q
```

### Source layout

```
src/
  shared/       constants.py (building address, room colours), activities.py, locations.py,
                amilia_client.py, calendar_client.py, event_store.py, …  (used by everything)
  webhook/      main.py (amilia_webhook, process_webhook_task), handlers.py, webhook_queue.py,
                reconcile_queue.py, retry_policy.py
  reconciler/   main.py (reconcile_activity)
scripts/        build.sh, backfill.py, bulk_delete.py, check_access.py, check_storage_access.py
test/
docs/           security notes and design notes
```
