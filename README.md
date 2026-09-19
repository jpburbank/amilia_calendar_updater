# Amilia Calendar Updater

## Deployment

If changes have been made or for some other reason the Cloud Function needs to be redeployed, and have passed tests, do the following.

### Dependencies
A Cloud Function deployment has the following requirements.

1. The GCloud CLI installed on the deployer's computer
2. A logged in account to the GCP project that the Function is being deployed to
3. The permissions to be able to deploy and start a Cloud Function

### Storage bucket setup
The event-ID mapping store (see `event_store.py`) needs a bucket, an IAM binding for the
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
`amilia_client.py` fetches per-occurrence schedule data (Start/End/Location) that Amilia's
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
`reconcile_queue.py`/`reconcile_worker.py`) rather than looping inline in the webhook handler,
since a Program's activity list can realistically run into the low hundreds. The queue is
Terraform-managed (`tasks.tf`) — run `terraform apply` there before deploying. Its ID is exposed
as the `reconcile_queue_id` output for `RECONCILE_QUEUE_ID` in `env.yaml`.

The queue also needs permission to invoke the reconciler function below, which means
**`amilia-activity-reconciler` must be deployed once before running `terraform apply` for the
invoker IAM binding to succeed** (see `tasks.tf`'s comment) — deploy it, then re-apply Terraform.

### CLI
In the root of the project run the following on the command line
.
```
gcloud functions deploy amilia-calendar-updater --gen2 --runtime=python312 --region=us-west1 \
    --source=. --entry-point=amilia_webhook --trigger-http --allow-unauthenticated \
    --service-account=amilia-calendar-updater@cm-calendar-506017.iam.gserviceaccount.com \
    --env-vars-file=env.yaml \
    --set-secrets="AMILIA_WEBHOOK_TOKEN=amilia-webhook-token:latest,AMILIA_API_USERNAME=amilia-api-username:latest,AMILIA_API_PASSWORD=amilia-api-password:latest"
```

The reconciliation worker is a second, separate Cloud Function — deploy it too (before the
command above, the first time, per the ordering note above):
```
gcloud functions deploy amilia-activity-reconciler --gen2 --runtime=python312 --region=us-west1 \
    --source=. --entry-point=reconcile_activity --trigger-http --no-allow-unauthenticated \
    --service-account=amilia-calendar-updater@cm-calendar-506017.iam.gserviceaccount.com \
    --env-vars-file=env.yaml
```
It shares the same source directory and `env.yaml` (it only reads `GOOGLE_CALENDAR_ID` and
`GOOGLE_EVENT_STORE_BUCKET` from it — the other entries are simply unused by this function).
`--no-allow-unauthenticated` is deliberate: only Cloud Tasks, via a signed identity token, should
ever be able to call it.
