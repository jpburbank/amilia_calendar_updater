"""
Thin wrapper around Google Cloud Storage, used as a small document store:
one JSON object per (context, external_id) pair, plus small marker objects
recording parent/child membership between two such documents (e.g. which
Activities belong to which Program).

Auth model: the same Cloud Function runtime service account already used by
CalendarClient, via Application Default Credentials. Unlike Calendar there
is no separate "sharing" step — IAM grants access to the bucket directly.

Setup: the bucket, its IAM binding to the function's runtime service account,
and its retention lifecycle rule(s) are all Terraform-managed in the sibling
amilia_calendar_updater_gcp_resources project (storage.tf) — run
`terraform apply` there before deploying this function. The bucket name is
exposed as that project's `amilia_calendar_event_mappings_bucket` output;
set it as GOOGLE_EVENT_STORE_BUCKET here.

Verify access once the bucket exists:  GOOGLE_EVENT_STORE_BUCKET=... python scripts/check_storage_access.py

No Cloud Storage API to enable — it's on by default for every project.

Object naming: "{context}/{external_id}.json", e.g. "FacilityBooking/FB-4821.json".
Namespaced by context (not just external_id) because Amilia's own ID
prefixes are reused across contexts (e.g. "PL-" appears in both
FacilityBooking and Registration).

A document's content is an arbitrary dict, stored as-is — callers decide
the shape (e.g. FacilityBooking stores a calendar_event_id; Activity stores
a whole map of occurrence IDs to calendar_event_ids). Passing `custom_time`
to set() sets the blob's Custom-Time metadata, so a bucket lifecycle rule
can expire objects a fixed number of days after the *underlying event* has
passed rather than after the object was last written. Custom-Time can only
increase once set (a GCS constraint), so set() takes the max of any
existing Custom-Time and the new value, ensuring an expiry date is never
pulled earlier. Not every context needs this — Program documents pass no
custom_time at all, since Programs are only ever removed by an explicit
Delete (archive) webhook, never auto-expired.

Membership markers (add_membership/remove_membership/list_members) record
that one document is a child of another, without ever reading or rewriting
the parent's own document — each marker is an independent object at
"{parent_context}/{parent_id}/{child_context}/{child_id}", so many children
can be added or removed concurrently with no shared state to race on, and
"who are my children" is a cheap prefix listing rather than a maintained
list embedded in the parent (which would need to survive concurrent writes
from siblings).
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

    def get(self, context: str, external_id: str) -> dict | None:
        """Returns the stored document, or None if unmapped."""
        try:
            data = self._blob(context, external_id).download_as_text()
        except NotFound:
            return None
        return json.loads(data)

    def set(
        self, context: str, external_id: str, fields: dict, custom_time: str | None = None
    ) -> None:
        """
        Writes/overwrites the document. `fields` is stored as given, plus an
        `updated_at` timestamp. `custom_time` (an ISO datetime string), when
        given, sets Custom-Time for the retention lifecycle rule.
        """
        blob = self._blob(context, external_id)
        if custom_time is not None:
            new_expiry = datetime.fromisoformat(custom_time)
            try:
                blob.reload()
                if blob.custom_time and blob.custom_time > new_expiry:
                    new_expiry = blob.custom_time  # never move Custom-Time earlier
            except NotFound:
                pass  # first write for this key — nothing to preserve
            blob.custom_time = new_expiry

        body = {**fields, "updated_at": datetime.now(timezone.utc).isoformat()}
        blob.upload_from_string(json.dumps(body), content_type="application/json")

    def delete(self, context: str, external_id: str) -> None:
        """Removes the document, if present. No-ops if already absent."""
        try:
            self._blob(context, external_id).delete()
        except NotFound:
            pass

    def add_membership(
        self, parent_context: str, parent_id: str, child_context: str, child_id: str
    ) -> None:
        """Records that (child_context, child_id) belongs to (parent_context, parent_id)."""
        self._bucket.blob(
            self._membership_name(parent_context, parent_id, child_context, child_id)
        ).upload_from_string(b"")

    def remove_membership(
        self, parent_context: str, parent_id: str, child_context: str, child_id: str
    ) -> None:
        """Removes the membership marker, if present. No-ops if already absent."""
        try:
            self._bucket.blob(
                self._membership_name(parent_context, parent_id, child_context, child_id)
            ).delete()
        except NotFound:
            pass

    def list_members(self, parent_context: str, parent_id: str, child_context: str) -> list[str]:
        """Returns the external_ids of every child recorded under this parent."""
        prefix = f"{parent_context}/{parent_id}/{child_context}/"
        return [blob.name[len(prefix) :] for blob in self._bucket.list_blobs(prefix=prefix)]

    def list_all(self, context: str) -> list[str]:
        """
        Returns the external_ids of every document stored under this context.
        Excludes membership markers, which live one level deeper (e.g.
        "Program/107638/Activities/111" is a marker, not a document, and is
        never mistaken for one here since it contains an extra "/" after the
        prefix and has no ".json" suffix).
        """
        prefix = f"{context}/"
        ids = []
        for blob in self._bucket.list_blobs(prefix=prefix):
            remainder = blob.name[len(prefix) :]
            if remainder.endswith(".json") and "/" not in remainder:
                ids.append(remainder[: -len(".json")])
        return ids

    def _blob(self, context: str, external_id: str):
        return self._bucket.blob(f"{context}/{external_id}.json")

    @staticmethod
    def _membership_name(
        parent_context: str, parent_id: str, child_context: str, child_id: str
    ) -> str:
        return f"{parent_context}/{parent_id}/{child_context}/{child_id}"
