"""
Thin wrapper around Google Cloud Storage, used as a small key-value store
mapping Amilia external IDs (e.g. FacilityBooking ReservationIds) to Google
Calendar event IDs. One object per (context, external_id) pair.

Auth model: the same Cloud Function runtime service account already used by
CalendarClient, via Application Default Credentials. Unlike Calendar there
is no separate "sharing" step — IAM grants access to the bucket directly.

Setup (done once per project):

  1. Create the bucket (uniform bucket-level access, matching the
     function's region):
       gcloud storage buckets create gs://<PROJECT_ID>-amilia-calendar-event-mappings \
         --location=<REGION> --uniform-bucket-level-access

  2. Grant the function's runtime service account object-level access,
     scoped to just this bucket:
       gcloud storage buckets add-iam-policy-binding \
         gs://<PROJECT_ID>-amilia-calendar-event-mappings \
         --member="serviceAccount:amilia-calendar-updater@<PROJECT_ID>.iam.gserviceaccount.com" \
         --role="roles/storage.objectAdmin"

  3. Apply the retention policy (see lifecycle.json in the repo root):
       gcloud storage buckets update gs://<PROJECT_ID>-amilia-calendar-event-mappings \
         --lifecycle-file=lifecycle.json

  4. Verify:  GOOGLE_EVENT_STORE_BUCKET=... python check_storage_access.py

No Cloud Storage API to enable — it's on by default for every project.

Object naming: "{context}/{external_id}.json", e.g. "FacilityBooking/FB-4821.json".
Namespaced by context (not just external_id) because Amilia's own ID
prefixes are reused across contexts (e.g. "PL-" appears in both
FacilityBooking and Registration), so a flat scheme risks collisions the
moment a second context (e.g. Activity) is wired up.

Each object's Custom-Time metadata is set to the underlying event's end
time, so a bucket-wide lifecycle rule can expire mappings a fixed number of
days after the *event* has passed, not after the object was last written —
see lifecycle.json. Custom-Time can only increase once set (a GCS
constraint), so set() takes the max of any existing Custom-Time and the
new end time, ensuring an expiry date is never pulled earlier.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from google.api_core.exceptions import NotFound
from google.cloud import storage


class EventStore:
    def __init__(self, bucket_name: str, client=None):
        self.bucket_name = bucket_name
        self._bucket = (client or storage.Client()).bucket(bucket_name)

    def get(self, context: str, external_id: str) -> str | None:
        """Returns the mapped Google Calendar event ID, or None if unmapped."""
        try:
            data = self._blob(context, external_id).download_as_text()
        except NotFound:
            return None
        return json.loads(data)["calendar_event_id"]

    def set(self, context: str, external_id: str, event_id: str, action: str, end_iso: str) -> None:
        """
        Writes/overwrites the mapping. `end_iso` is the underlying event's end
        time, used as Custom-Time so the retention lifecycle rule can expire
        this object relative to when the *event* ends, not when it was written.
        """
        blob = self._blob(context, external_id)
        new_expiry = datetime.fromisoformat(end_iso)
        try:
            blob.reload()
            if blob.custom_time and blob.custom_time > new_expiry:
                new_expiry = blob.custom_time  # never move Custom-Time earlier
        except NotFound:
            pass  # first write for this key — nothing to preserve

        blob.custom_time = new_expiry
        body = {
            "calendar_event_id": event_id,
            "context": context,
            "external_id": external_id,
            "action": action,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        blob.upload_from_string(json.dumps(body), content_type="application/json")

    def delete(self, context: str, external_id: str) -> None:
        """Removes the mapping, if present. No-ops if already absent."""
        try:
            self._blob(context, external_id).delete()
        except NotFound:
            pass

    def _blob(self, context: str, external_id: str):
        return self._bucket.blob(f"{context}/{external_id}.json")
