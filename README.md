# Amilia Calendar Updater

Open security issues (identified but not yet fixed) are tracked in
[`docs/security-notes.md`](docs/security-notes.md).

## What gets synced

Amilia **Programs**, **Activities**, and each Activity's **occurrences** (one calendar event per
session) are synced to Google Calendar.

**FacilityBookings are not synced by default.** They were part of the original requirements, but
as of 2026-11 the calendar no longer needs them. The code is kept and tested, not deleted, behind
a feature flag:

| Env var (`env.yaml`) | Default | Effect |
|---|---|---|
| `FACILITY_BOOKING_SYNC_ENABLED` | `"false"` | `"true"` routes FacilityBooking webhooks to `handle_facility_booking` and makes `scripts/backfill.py` import reservations. Off: FacilityBooking webhooks are logged and answered `status=ignored` (HTTP 200), and the backfill skips `/reservations` entirely. Any value other than `true`/`false` makes the function fail at startup. |

Turning it on or off takes effect on the next deploy. Turning it off leaves existing
FacilityBooking calendar events and event-store documents in place: they are no longer updated
or deleted, so clear them manually if they shouldn't stay. The flag lives in
`src/shared/feature_flags.py`.

**Titles are trimmed.** Leading and trailing whitespace is stripped from every Program/Activity
name and FacilityBooking title before it's stored or used as a calendar event title (Amilia keeps
stray spaces exactly as typed, e.g. `"Bandsaw Bear Claws  "`). A blank title falls back to the
default (`Activity`, `Facility Booking`). Titles are display-only; nothing looks a record up by
name. Events created earlier keep their untrimmed title until their Activity is next updated or
re-synced. See `src/shared/titles.py`.

**Event locations come from each occurrence.** A location belongs to an occurrence, not an
Activity, and the Activity webhook never carries one. Each Activity event's location is the
occurrence's `Location.Name` (REST `activities/{id}/occurrences`), falling back to the distinct
names in the Activity's `Schedules[].Locations[].Name` (REST `activities/{id}`). It's stored per
occurrence, so the reconciler reuses it without another REST call. Events created earlier have
no location until their Activity is next updated or re-synced. See `src/shared/locations.py`.

**Redelivered and out-of-order webhooks are skipped.** Amilia delivers at least once and can
replay an old webhook minutes later, after newer ones (seen live: a redelivered Activity Create
reverted a rename that had already synced). Each event-store document records the `EventTime` of
the webhook that last wrote it (`last_event_time`); a webhook whose `EventTime` isn't newer is
answered `status=skipped reason=stale event` (HTTP 200) and changes nothing. A webhook with no
usable `EventTime`, or a document without `last_event_time` (e.g. written by the backfill), is
processed normally.

**Deleted, archived, and hidden records are checked against Amilia.** `EventTime` can only order a
webhook against a stored document, so Activity and Program Create/Update also ask Amilia's REST API
for the record's current state:

| Amilia says | Result |
|---|---|
| Activity or Program is 404 (deleted) | `status=skipped reason=deleted in Amilia`, nothing changes |
| Program is `IsArchived: true` (archived Programs are 200, not 404) | `status=skipped reason=archived in Amilia` |
| Activity's current `Status` (archived Activities are 200 with `Status: "Hidden"`) | Used for visibility instead of the webhook's `Status` |
| Lookup fails any other way (timeout, 5xx) | In the background worker: retried while the 24 h window lasts. On the final attempt, or when processing inline because enqueueing failed: warning logged and the webhook payload used (fail open), so an Amilia outage doesn't lose changes |

That's one extra REST call per Activity webhook and per (rare) Program webhook. FacilityBooking
(off by default) has no such check.

## Deployment

If changes have been made or for some other reason the Cloud Function needs to be redeployed, and have passed tests, do the following.

### Deployment variables
Every command in this README uses shell variables instead of real project values, so nothing
environment-specific is baked into the docs or code. Set them once per shell, from the
Terraform project's outputs and your Amilia org:
```
cd ../amilia_calendar_updater_gcp_resources
export REGION=$(terraform output -raw function_region)
export SA_EMAIL=$(terraform output -raw amilia_calendar_updater_email)
cd -
export AMILIA_ORG_ID=<Amilia OrganizationId>
```
The functions themselves read their configuration from `env.yaml` (see `env.yaml.example`) and
Secret Manager (`--set-secrets`), never from constants in the code. `env.yaml` holds the
project ID, bucket, Cloud Tasks queue ID/region/worker URL, the reconciler invoker identity
(`RECONCILE_INVOKER_SERVICE_ACCOUNT`, normally `$SA_EMAIL`), and feature flags.

### Dependencies
A Cloud Function deployment has the following requirements.

1. The GCloud CLI installed on the deployer's computer
2. A logged in account to the GCP project that the Function is being deployed to
3. The permissions to be able to deploy and start a Cloud Function

### Running scripts locally
Every script under `scripts/` (and the test suite) depends on packages installed in this repo's
own `.venv`, not whatever `python`/`python3` resolves to system-wide — invoking a script with a
bare `python` reliably fails with `ModuleNotFoundError: No module named 'google'` (or similar).
Either activate it once per shell (`source .venv/bin/activate`, then `python` and `pytest` resolve
correctly for the rest of that session) or invoke `.venv/bin/python` directly each time, as the
commands below do.

`.venv` should be built from Python 3.12 (see `.python-version`), matching both Cloud Functions'
`--runtime=python312` and each other — testing locally against a different Python version than
what's actually deployed defeats the point of running the test suite before deploying. Rebuild it
if it's ever on the wrong version (`.venv/bin/python -V` to check):
```
brew install python@3.12
rm -rf .venv
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

### Linting and formatting
[Ruff](https://docs.astral.sh/ruff/) is both the formatter and the linter, configured in
`pyproject.toml` (100-column lines; generated `shared/` copies excluded). It's installed from
`requirements-dev.txt`. Run both before every commit, along with the tests:
```
.venv/bin/ruff format .
.venv/bin/ruff check .        # add --fix to apply the safe autofixes
.venv/bin/python -m pytest -q
```

### Storage bucket setup
The event-ID mapping store (see `src/shared/event_store.py`) needs a bucket, an IAM binding for the
function's service account, and a retention lifecycle applied. This is Terraform-managed in the
sibling `amilia_calendar_updater_gcp_resources` project (`storage.tf`) — run `terraform apply`
there before deploying this function, or after changing the retention window
(`event_mapping_retention_days` variable, default 90 days). The bucket name is exposed as the
`amilia_calendar_event_mappings_bucket` output for `GOOGLE_EVENT_STORE_BUCKET` in `env.yaml`.

### Webhook token setup
Amilia has no webhook-signing mechanism, so requests are authenticated by a shared-secret token
instead (checked first thing in `amilia_webhook`). The token is Terraform-managed in the sibling
`amilia_calendar_updater_gcp_resources` project (`secrets.tf`) — run `terraform apply` there
before deploying. Register the webhook with Amilia using this function's URL with the token
appended as a query param, e.g. `?token=<value>`, fetched with:
```
gcloud secrets versions access latest --secret=amilia-webhook-token
```

### Target calendar (switching between dev and prod)
The calendar both functions write to is the `google-calendar-id` secret, not an `env.yaml`
entry, so switching calendars (the current one is dev-only; production will use a different one)
needs no code or config change. The secret is Terraform-managed in the sibling
`amilia_calendar_updater_gcp_resources` project (`secrets.tf`, container only); the value is
set by hand. To switch:

1. Share the new calendar with the function's service account ("Make changes to events") and
   check it with `scripts/check_access.py` (see `src/shared/calendar_client.py`).
2. Add the new ID as a new secret version:
   ```
   printf '%s' '<calendar-id>@group.calendar.google.com' | \
     gcloud secrets versions add google-calendar-id --data-file=-
   ```
3. Redeploy **both** functions (commands under [CLI](#cli)). Secrets mounted as env vars are
   read when an instance starts, so a redeploy is what guarantees every instance switches.
4. **Reset the event store.** Its documents hold calendar event IDs from the *old* calendar,
   so an Update or Delete would target events that don't exist on the new one. Clear the
   bucket's documents and re-run the backfill against the new calendar. This is a manual,
   deliberate step.

To run a script against the configured calendar, read the ID from the secret:
`GOOGLE_CALENDAR_ID=$(gcloud secrets versions access latest --secret=google-calendar-id)`.

### Amilia REST API credentials (Program/Activity sync)
`src/webhook/amilia_client.py` fetches per-occurrence schedule data (Start/End/Location) that Amilia's
webhooks don't carry, plus each Activity's and Program's current state (see "What gets synced"), using a dedicated Amilia user account rather than the runtime service
account (Amilia has no concept of a GCP identity). Terraform creates the
`amilia-api-username` / `amilia-api-password` secret *containers* only — it cannot set real
Amilia credentials as values. After creating that dedicated user in Amilia, set the values by
hand:
```
printf '%s' '<username>' | gcloud secrets versions add amilia-api-username --data-file=-
printf '%s' '<password>' | gcloud secrets versions add amilia-api-password --data-file=-
```

### Cloud Tasks queue setup (Program/Activity sync)
A Program's `Online` flag flipping fans out to one async task per child Activity (via
`src/webhook/reconcile_queue.py`/`src/reconciler/main.py`) rather than looping inline in the
webhook handler, since a Program's activity list can realistically run into the low hundreds. The queue is
Terraform-managed (`tasks.tf`) — run `terraform apply` there before deploying. Its ID is exposed
as the `reconcile_queue_id` output for `RECONCILE_QUEUE_ID` in `env.yaml`.

The queue also needs permission to invoke the reconciler function below, which means
**`amilia-activity-reconciler` must be deployed once before running `terraform apply` for the
invoker IAM binding to succeed** (see `tasks.tf`'s comment) — deploy it, then re-apply Terraform.

### Backfill (Program/Activity/FacilityBooking sync)
Webhooks only deliver *changes* — anything created in Amilia before the corresponding webhook
subscription existed will never get a `Create` webhook. `scripts/backfill.py` is a one-time,
standalone bulk loader that seeds the event store (and creates matching calendar events) for:
- every Program,
- every Activity whose last occurrence hasn't ended more than `--lookback-days` (default 30) in
  the past — no forward bound, so anything upcoming is always included, and
- every FacilityBooking, **only when `FACILITY_BOOKING_SYNC_ENABLED` is `"true"`** (see
  [What gets synced](#what-gets-synced); otherwise this phase is skipped). These come from
  Amilia's `/reservations` endpoint, which covers the AdminBooking, FacilityBooking, and
  PrivateLesson reservation types, matching what the live webhook treats as one
  `FacilityBooking` context. Only reservations with a start between `--lookback-days` in the past and
  `--reservation-lookahead-days` (default 730, ~2 years) in the future. Unlike Activities, this
  endpoint requires an explicit forward bound server-side, so there's no true "unbounded" option
  here — `--reservation-lookahead-days` is a practical stand-in. Cancelled reservations are
  skipped, and so is a reservation `Type` of `Activity` — that's a booked *session* of an Activity,
  not a standalone facility use, already fully covered by the Activity backfill above; importing
  it here too would duplicate that occurrence's calendar event (confirmed in production before
  this exclusion: 251 of 457 FacilityBooking documents had an `AC-` prefix, every one a duplicate).

Every Activity calendar event's description is built from Amilia's `Description`/`Note`/
`Prerequisite` fields, each its own paragraph — `Description` has no header (Amilia already sends
it as HTML), `Note` and `Prerequisite` each get one, and either is omitted entirely when null or
empty. Same logic in both the live webhook (`handle_activity`) and the backfill script, each with
its own copy (`_build_activity_description` / `build_activity_description`) per the
no-cross-import rule above.

Every Activity and FacilityBooking calendar event also carries two
`extendedProperties.private` fields — `amilia_type` (`"Activity"` or `"FacilityBooking"`) and
`amilia_type_id` (the Activity's own `Id`, or the FacilityBooking's `ReservationId`) — so the event
itself identifies what it is in Amilia without needing the event store. These aren't shown
anywhere in the Calendar UI, and "private" only means they don't propagate to other attendees'
copies of the event — any caller with ordinary read access to the event via the API can still read
them, there's no per-app access restriction.

Safe to re-run (won't duplicate calendar events already recorded). Deliberately doesn't import
anything from `src/webhook/` — see its own module docstring for why. Run once, before relying on
live sync, impersonating the function's own service account:
```
gcloud auth application-default login \
  --impersonate-service-account=$SA_EMAIL

GOOGLE_CALENDAR_ID=$(gcloud secrets versions access latest --secret=google-calendar-id) \
GOOGLE_EVENT_STORE_BUCKET=... PYTHONPATH=src .venv/bin/python scripts/backfill.py \
  --org-id $AMILIA_ORG_ID --amilia-username <user> --amilia-password <pass> --dry-run
```
Drop `--dry-run` once the preview output looks right.

### Bulk delete (testing / emergency cleanup)
`scripts/bulk_delete.py` deletes event-store documents (and optionally the
calendar events they reference) in bulk — by explicit ID, by parent Program,
by age, or everything in a context. Used both for clearing out repeated test
data and, if it's ever needed, emergency cleanup against production, so it's
deliberately more paranoid than the backfill script: dry-run by default,
requires `--execute` plus a `--confirm-bucket` that must exactly match the
target bucket, refuses to run past `--limit` documents, and writes a
timestamped audit log under `audit_logs/` (gitignored) as it goes. Full usage
and examples: [`scripts/BULK_DELETE.md`](scripts/BULK_DELETE.md).

### Source layout
Two separately deployed Cloud Functions, each its own self-contained directory:

```
src/
  shared/       calendar_client.py, event_store.py — the single source of truth for both
  webhook/      main.py (amilia_webhook) + handlers.py, amilia_client.py, reconcile_queue.py
  reconciler/   main.py (reconcile_activity) — the Cloud Tasks worker
scripts/        check_access.py, check_storage_access.py, build.sh, backfill.py, bulk_delete.py
test/
```

Cloud Functions' buildpack deploy always imports a file named `main.py` from whatever `--source`
points at, so the two functions can't share one source tree even though they share code —
`src/shared/` is copied into `src/webhook/shared/` and `src/reconciler/shared/` by
`scripts/build.sh`, which **must be run before every deploy** (those two copies are generated and
gitignored, not hand-maintained).

#### Why `scripts/build.sh` has to run before every deploy

`gcloud functions deploy --source=<dir>` uploads *exactly* the contents of `<dir>` — nothing
outside it. `src/webhook/main.py` and `src/reconciler/main.py` both do
`from shared.calendar_client import CalendarClient`, which needs a real `shared/` subdirectory
physically present inside their own directory at upload time. The canonical `shared/` code lives
at `src/shared/`, a *sibling* of both — outside what either deploy uploads — so gcloud has no way
to see it unless a copy has already been placed inside `src/webhook/` and `src/reconciler/`.
`scripts/build.sh` is that copy step, nothing more.

Skipping it fails in one of two ways:
- **`shared/` doesn't exist yet** (fresh clone, or it was cleaned) → deploy fails at container
  startup with `ModuleNotFoundError: No module named 'shared'` (we hit this deploying the
  reconciler the first time).
- **`shared/` exists but is stale** — if `src/shared/calendar_client.py` or `event_store.py`
  changes and you deploy without re-running `build.sh`, you silently ship the *old* shared code.
  No error, just wrong behavior.

**Why not a symlink instead** (`src/webhook/shared -> ../shared`), so it's always in sync with no
separate step? Symbolic links don't reliably survive cloud source-staging — this is a known,
recurring problem across GCP/Firebase function deployments in general (not just this project),
and the standard workaround people converge on is exactly this copy-at-build-time approach.
Structurally, a symlink here would point at `../shared`, a path *outside* the uploaded directory
— if gcloud preserves it as a symlink rather than resolving it locally, that target won't exist
on Google's remote build environment (only `--source`'s contents get uploaded), reproducing the
same `ModuleNotFoundError` non-deterministically. Not worth testing empirically against
undocumented platform behavior when the copy-script approach is simple and already proven to
work.

### CLI
From the root of the project:
```
./scripts/build.sh

gcloud functions deploy amilia-calendar-updater --gen2 --runtime=python312 --region=$REGION \
    --source=src/webhook --entry-point=amilia_webhook --trigger-http --allow-unauthenticated \
    --service-account=$SA_EMAIL \
    --env-vars-file=env.yaml \
    --set-secrets="GOOGLE_CALENDAR_ID=google-calendar-id:latest,AMILIA_WEBHOOK_TOKEN=amilia-webhook-token:latest,AMILIA_API_USERNAME=amilia-api-username:latest,AMILIA_API_PASSWORD=amilia-api-password:latest"
```

The reconciliation worker is a second, separate Cloud Function — deploy it too (before the
command above, the first time, per the ordering note above):
```
./scripts/build.sh

gcloud functions deploy amilia-activity-reconciler --gen2 --runtime=python312 --region=$REGION \
    --source=src/reconciler --entry-point=reconcile_activity --trigger-http --no-allow-unauthenticated \
    --service-account=$SA_EMAIL \
    --env-vars-file=env.yaml \
    --set-secrets="GOOGLE_CALENDAR_ID=google-calendar-id:latest"
```
It shares the repo's one `env.yaml` (it only reads `GOOGLE_EVENT_STORE_BUCKET` from it, plus
`GOOGLE_CALENDAR_ID` from the secret — the other entries are simply unused by this function), but
has its own, smaller `src/reconciler/requirements.txt` — it carries none of the webhook-only
dependencies (Cloud Tasks client, `requests`). `--no-allow-unauthenticated` is deliberate: only
Cloud Tasks, via a signed identity token, should ever be able to call it.

The webhook worker is a third Cloud Function: the **same source** as the webhook, with its own
entry point, and private (see "Background webhook processing" below):
```
./scripts/build.sh

gcloud functions deploy amilia-webhook-worker --gen2 --runtime=python312 --region=$REGION \
    --source=src/webhook --entry-point=process_webhook_task --trigger-http --no-allow-unauthenticated \
    --service-account=$SA_EMAIL \
    --env-vars-file=env.yaml \
    --set-secrets="GOOGLE_CALENDAR_ID=google-calendar-id:latest,AMILIA_WEBHOOK_TOKEN=amilia-webhook-token:latest,AMILIA_API_USERNAME=amilia-api-username:latest,AMILIA_API_PASSWORD=amilia-api-password:latest"
```
Any change under `src/webhook/` or `src/shared/` must be deployed to **both**
`amilia-calendar-updater` and `amilia-webhook-worker`, since they share code.

### Background webhook processing
`amilia-calendar-updater` (public) only checks the token, enqueues the raw webhook on the
`amilia-webhook-processing` Cloud Tasks queue, and answers `status=queued` (HTTP 200) in
milliseconds. On 2026-11-13, replies that took 2.7–5.9 s were followed by Amilia redeliveries.
`amilia-webhook-worker` (private, called only by Cloud Tasks) runs the handlers.

| Worker outcome | HTTP | What happens next |
|---|---|---|
| `ok` / `skipped` / `ignored` | 200 | Done |
| `retrying`: transient failure (network errors, timeouts, 408/429/5xx, Calendar rate limits; see `src/webhook/retry_policy.py`) | 503 | Cloud Tasks retries: backoff 10 s, doubling, capped at 10 min |
| `discarded`: permanent failure (e.g. calendar not shared, bad credentials, malformed payload), or any failure once `WEBHOOK_TASK_GIVE_UP_AFTER_SECONDS` (24 h) have passed since enqueue | 200 | Gives up. **One ERROR log entry holds the full webhook JSON, the error, and the traceback**, so the change can be redone by hand |

- **One task at a time.** The queue has `max_concurrent_dispatches = 1`, so two tasks for the
  same record can never race. Volume is a few dozen webhooks a day.
- **The worker gives up on its own.** With retries left, a failed Amilia current-state lookup is
  retried rather than falling back to the webhook payload. Only the final attempt (≥ 24 h after
  enqueue) falls back, so a long Amilia outage still applies the change.
- **24 h vs. 25 h.** The queue's own 25 h `max_retry_duration` (Terraform
  `webhook_task_max_retry_duration`) is only a backstop and must stay above
  `WEBHOOK_TASK_GIVE_UP_AFTER_SECONDS`.
- **Retries never duplicate calendar events.** Events are created under IDs derived from Amilia
  IDs (`stable_event_id` in `src/shared/calendar_client.py`). A repeated create gets a 409 and
  patches the existing event instead, restoring it if it had been deleted.
- **If enqueueing fails**, the webhook processes inline as before (`status=ok`/`dropped`).

Find discarded tasks with `jsonPayload.message:"status=discarded"` (or `textPayload:` if entries
land as text). Emailing them is [issue #26](https://github.com/jpburbank/amilia_calendar_updater/issues/26).

**First rollout order** (the worker's invoker grant needs the function to exist):
1. In `amilia_calendar_updater_gcp_resources`:
   `terraform apply -target=google_cloud_tasks_queue.webhook_processing -target=google_cloud_tasks_queue_iam_member.amilia_calendar_updater_webhook_enqueuer`
2. Add `WEBHOOK_QUEUE_ID`, `WEBHOOK_WORKER_URL`, and `WEBHOOK_TASK_GIVE_UP_AFTER_SECONDS` to
   `env.yaml` (see `env.yaml.example`).
3. Deploy `amilia-webhook-worker`.
4. Full `terraform apply` (creates the invoker grant).
5. Deploy `amilia-calendar-updater`. It starts enqueueing from this point.
