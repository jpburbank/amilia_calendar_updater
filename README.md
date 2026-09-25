# Amilia Calendar Updater

## Deployment

If changes have been made or for some other reason the Cloud Function needs to be redeployed, and have passed tests, do the following.

### Dependencies
A Cloud Function deployment has the following requirements.

1. The GCloud CLI installed on the deployer's computer
2. A logged in account to the GCP project that the Function is being deployed to
3. The permissions to be able to deploy and start a Cloud Function

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

### Amilia REST API credentials (Program/Activity sync)
`src/webhook/amilia_client.py` fetches per-occurrence schedule data (Start/End/Location) that Amilia's
webhooks don't carry, using a dedicated Amilia user account rather than the runtime service
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

### Backfill (Program/Activity sync)
Webhooks only deliver *changes* — anything created in Amilia before the `Program`/`Activity`
webhook subscriptions existed will never get a `Create` webhook. `scripts/backfill.py` is a
one-time, standalone bulk loader that seeds the event store (and creates matching calendar
events) for every Program and every Activity whose last occurrence hasn't ended more than
`--lookback-days` (default 30) in the past — no forward bound, so anything upcoming is always
included. Safe to re-run (won't duplicate calendar events already recorded). Deliberately doesn't
import anything from `src/webhook/` — see its own module docstring for why. Run once, before
relying on live Program/Activity sync, impersonating the function's own service account:
```
gcloud auth application-default login \
  --impersonate-service-account=amilia-calendar-updater@cm-calendar-506017.iam.gserviceaccount.com

GOOGLE_CALENDAR_ID=... GOOGLE_EVENT_STORE_BUCKET=... PYTHONPATH=src python scripts/backfill.py \
  --org-id 17659 --amilia-username <user> --amilia-password <pass> --dry-run
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

gcloud functions deploy amilia-calendar-updater --gen2 --runtime=python312 --region=us-west1 \
    --source=src/webhook --entry-point=amilia_webhook --trigger-http --allow-unauthenticated \
    --service-account=amilia-calendar-updater@cm-calendar-506017.iam.gserviceaccount.com \
    --env-vars-file=env.yaml \
    --set-secrets="AMILIA_WEBHOOK_TOKEN=amilia-webhook-token:latest,AMILIA_API_USERNAME=amilia-api-username:latest,AMILIA_API_PASSWORD=amilia-api-password:latest"
```

The reconciliation worker is a second, separate Cloud Function — deploy it too (before the
command above, the first time, per the ordering note above):
```
./scripts/build.sh

gcloud functions deploy amilia-activity-reconciler --gen2 --runtime=python312 --region=us-west1 \
    --source=src/reconciler --entry-point=reconcile_activity --trigger-http --no-allow-unauthenticated \
    --service-account=amilia-calendar-updater@cm-calendar-506017.iam.gserviceaccount.com \
    --env-vars-file=env.yaml
```
It shares the repo's one `env.yaml` (it only reads `GOOGLE_CALENDAR_ID` and
`GOOGLE_EVENT_STORE_BUCKET` from it — the other entries are simply unused by this function), but
has its own, smaller `src/reconciler/requirements.txt` — it carries none of the webhook-only
dependencies (Cloud Tasks client, `requests`). `--no-allow-unauthenticated` is deliberate: only
Cloud Tasks, via a signed identity token, should ever be able to call it.
