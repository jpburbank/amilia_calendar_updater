"""
Diagnostic: confirms the current credentials can actually read, write, and
delete objects in GOOGLE_EVENT_STORE_BUCKET. Run it after creating the
bucket and granting the service account access, before pointing Amilia at
the deployed function.

Not imported by the function — this is a manual, run-it-yourself script.

As the function's own identity (the check that matters):
    gcloud auth application-default login \
      --impersonate-service-account=<SA_EMAIL>
    GOOGLE_EVENT_STORE_BUCKET=... python check_storage_access.py

Impersonating needs roles/iam.serviceAccountTokenCreator on that service
account. Running it as yourself instead only tells you the bucket exists —
your own access says nothing about the service account's.

Writes, reads back, and deletes a probe object.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import google.auth
import google.auth.transport.requests
from google.api_core.exceptions import Forbidden, NotFound
from google.cloud import storage

PROBE_OBJECT_NAME = "check_storage_access_probe.json"
PROBE_BODY = '{"note": "amilia-calendar-updater access check (safe to delete)"}'


def _identity(credentials) -> str:
    """Best-effort email for whoever we authenticated as."""
    email = getattr(credentials, "service_account_email", None)
    if not email or email == "default":
        # Compute/Cloud Run credentials only learn their own email on refresh.
        credentials.refresh(google.auth.transport.requests.Request())
        email = getattr(credentials, "service_account_email", None)
    return email or "<not a service account — probably your user account>"


def _explain(exc: Exception, bucket_name: str) -> str:
    if isinstance(exc, NotFound):
        return (
            f"404: bucket '{bucket_name}' was not found — either the name is wrong or it hasn't "
            "been created yet: gcloud storage buckets create gs://<bucket> --location=<REGION> "
            "--uniform-bucket-level-access"
        )
    if isinstance(exc, Forbidden):
        return (
            "403: the bucket exists but this identity can't access it. Grant it, scoped to just "
            "this bucket:\n"
            "  gcloud storage buckets add-iam-policy-binding gs://<bucket> \\\n"
            '    --member="serviceAccount:<SA_EMAIL>" --role="roles/storage.objectAdmin"'
        )
    return str(exc)


def main() -> int:
    bucket_name = os.environ.get("GOOGLE_EVENT_STORE_BUCKET")
    if not bucket_name:
        print("GOOGLE_EVENT_STORE_BUCKET is not set", file=sys.stderr)
        return 2

    credentials, _ = google.auth.default()
    print(f"Authenticating as: {_identity(credentials)}")
    print(f"Target bucket:     {bucket_name}")

    bucket = storage.Client(credentials=credentials).bucket(bucket_name)
    blob = bucket.blob(PROBE_OBJECT_NAME)
    blob.custom_time = datetime.now(timezone.utc) + timedelta(days=1)

    try:
        blob.upload_from_string(PROBE_BODY, content_type="application/json")
    except (NotFound, Forbidden) as exc:
        print(_explain(exc, bucket_name), file=sys.stderr)
        return 1
    print("Write access: OK")

    try:
        blob.download_as_text()
    except (NotFound, Forbidden) as exc:
        print(_explain(exc, bucket_name), file=sys.stderr)
        return 1
    print("Read access:  OK")

    try:
        blob.delete()
    except (NotFound, Forbidden) as exc:
        print(
            f"WARNING: could not clean up probe object {PROBE_OBJECT_NAME} ({exc}). "
            "Delete it by hand.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
