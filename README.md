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

### CLI
In the root of the project run the following on the command line
.
```
gcloud functions deploy amilia-calendar-updater --gen2 --runtime=python312 --region=us-west1 \
    --source=. --entry-point=amilia_webhook --trigger-http --allow-unauthenticated \
    --service-account=amilia-calendar-updater@cm-calendar-506017.iam.gserviceaccount.com \
    --env-vars-file=env.yaml
```
