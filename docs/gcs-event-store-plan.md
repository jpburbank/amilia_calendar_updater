# GCS-backed event-ID mapping store

> **Update (post-implementation):** the bucket, its IAM binding, and its retention lifecycle rule described below as manual `gcloud` commands / a checked-in `lifecycle.json` were superseded by Terraform, managed in the sibling `amilia_calendar_updater_gcp_resources` project (`storage.tf`). The design rationale below (object naming, granularity, Custom-Time-based retention) is unchanged — only how the bucket is provisioned moved.

## Context

`handlers.py` can create Google Calendar events for `FacilityBooking` webhooks, but Update and Delete don't work: there's no persistence layer mapping Amilia's `ReservationId` to the Google Calendar `event_id` that was created for it. A stub, `_lookup_event_id()`, always returns `None`, so every Update/Delete just no-ops with `"skipped": "no stored event mapping"`.

Firestore, Cloud SQL, and Memorystore were priced out and rejected (Firestore for per-operation cost-overrun risk as usage grows; the others for a non-zero always-on cost floor regardless of usage). **Google Cloud Storage** was chosen instead: one small JSON object per booking ID, holding the mapped calendar event ID. It has a genuine $0 floor (pure pay-per-use, generous Always Free tier) and this app only ever needs exact-key lookups, never queries — exactly what GCS objects are good at.

Scope for this change: only `FacilityBooking` is wired up. **Registration webhooks are being ignored entirely for now** (confirmed with the user) — `handle_registration` is deleted rather than kept as dead code. **Amilia Activity webhooks (create/edit/delete) are a known near-future need**, not being implemented now, but the storage layer's key scheme is designed today so adding `Activity` support later needs no redesign or data migration.

## Bucket & object-naming strategy

**One bucket for everything**, not one bucket per context/type. A separate bucket per context would mean a separate bucket-creation command and a separate IAM binding to keep in sync for every new webhook type added over time, for no real benefit — this is fundamentally one small key-value store, and GCS's own idiomatic pattern for this is prefixes-as-folders within a single bucket, not bucket-per-namespace.

**Object naming is namespaced by context**: `{context}/{external_id}.json`, e.g. `FacilityBooking/FB-4821.json`, later `Activity/AC-9001.json`. This directly anticipates the Activity work: Amilia's own ID scheme reuses prefixes across contexts (e.g. `PL-` appears in both `FacilityBooking` and `Registration`), so a flat `{external_id}.json` scheme would risk a collision the moment a second context is added. Namespacing costs nothing today and avoids a rename/migration later.

A `matchesPrefix` condition (see **Retention** below) also lets each context prefix have its own retention rule later, without redesigning anything, if e.g. Activities ever need a different grace period than bookings.

## Object granularity: one object per event

**Recommendation: one GCS object per booking**, i.e. per `(context, external_id)` pair — not one aggregated file holding all mappings.

**Pros:**
- **No read-modify-write races.** Each write only ever touches its own key. Cloud Functions gen2 can scale to multiple concurrent instances; with one aggregate file, two bookings created at the same moment would both read-modify-write the same object, and one write would silently clobber the other. Per-object writes make that structurally impossible.
- **O(1) lookups and writes**, not O(total mappings). An aggregate file needs a full read + full rewrite for every single Create/Update/Delete regardless of how many other bookings exist; per-object access doesn't degrade as the store grows.
- **Deletion is exact and trivial** — delete one object, done. No parse-modify-rewrite step.
- **Debuggable in place** — `gsutil cat gs://<bucket>/FacilityBooking/FB-4821.json` shows exactly one booking's state; an aggregate file requires downloading and grepping the whole thing.
- **Per-object expiry is possible at all** (see Retention below) — GCS Lifecycle Management operates on individual objects' metadata. An aggregate file has one age for the entire file, not per-entry, so it *cannot* support "expire this one booking's mapping 90 days after its event date" — only the whole-file approach would be incompatible with the retention design below.

**Cons:**
- More total objects in the bucket. Not a real cost here: GCS bills per-operation and per-GB, not per-object-count, and scales to billions of objects without issue.
- Marginally more write operations than batching many mappings into fewer file writes — still effectively free at this app's volume (see the earlier GCS pricing comparison).

There is no real scenario where the aggregate-file alternative wins for this workload — it's strictly worse on correctness (races) and is incompatible with per-booking expiry.

## Retention: expire mappings after the event has passed, not after last write

**Key requirement from the user: a mapping must not be deleted before the Amilia event's own date has passed — including a booking made far in the future — but growth still needs a hard bound.**

Plain GCS Object Lifecycle Management only understands an object's own age (time since it was created/last modified) — it has no idea what "the booking's end date" means. GCS has a purpose-built feature for exactly this mismatch: **`Custom-Time` object metadata** — a timestamp *you* set on the object — paired with a lifecycle condition, **`daysSinceCustomTime`**, that measures age from that timestamp instead of the object's real write time.

**Design:**
- When `EventStore.set(...)` writes an object (on both Create and Update), it also sets the blob's `custom_time` to the booking's `End` date/time from the Amilia payload.
- A lifecycle rule like `{"action": {"type": "Delete"}, "condition": {"daysSinceCustomTime": 90}}` then deletes the object only once **90 days after the booking's own end date** has passed — never before, regardless of how far in the future the booking was made when created. 90 is a starting default (see below) and is a one-line config change to retune later — no code change, no redeploy.
- **Constraint to design around**: GCS enforces that `Custom-Time` can only increase, never decrease, once set. If an Update ever moves a booking's `End` *earlier* (e.g. a shortened reservation), `set()` must not attempt to set `custom_time` backward. Handle this by reading the object's existing `custom_time` (a cheap metadata reload, only needed on Update — Create has no prior object) and writing `max(existing_custom_time, new_end_date)`. Net effect: the mapping's expiry only ever gets pushed later or stays the same, never earlier than originally scheduled — safe by construction, never causes a premature delete.
- This expiry is a **backstop**, not a replacement for explicit deletion: a real Amilia `Delete` webhook still deletes the object immediately (already in the plan below). The lifecycle rule only cleans up bookings that are never explicitly cancelled/deleted — e.g. ordinary bookings that simply complete with no cancellation event.

**Schedule**: GCS's lifecycle engine runs as a background scan roughly once per day (Google-managed, not something this app schedules or invokes) — an object becomes eligible the instant it crosses the `daysSinceCustomTime` threshold, and is swept away on the next daily pass, typically within ~24h of becoming eligible.

**Default grace period: 90 days after the booking's `End` date.** This is a single number in a checked-in `lifecycle.json`, applied with one `gcloud storage buckets update --lifecycle-file=...` command — trivial to change later (e.g. to 30 or 365) without touching any code.

`lifecycle.json` (new file, applied to the bucket once at setup, and again any time the retention window changes):
```json
{
  "rule": [
    {
      "action": {"type": "Delete"},
      "condition": {
        "daysSinceCustomTime": 90,
        "matchesPrefix": ["FacilityBooking/"]
      }
    }
  ]
}
```
Scoped with `matchesPrefix` rather than bucket-wide so that when `Activity/` objects are added later, they can get their own rule (and their own retention window, if it should differ) added to this same file without touching the `FacilityBooking/` rule.

## Implementation

### 1. New module: `event_store.py`

```python
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
```

**Client library: `google-cloud-storage`** (the idiomatic client), not `googleapiclient.discovery.build("storage", "v1", ...)`. The existing `googleapiclient` usage in `calendar_client.py` exists only because Calendar has no idiomatic client — that constraint doesn't apply to GCS, which has a first-class idiomatic library that's meaningfully simpler here (typed `NotFound`, no multipart/media-upload boilerplate, and native `custom_time` support on the `Blob` object).

**No new error handling beyond the `NotFound`→`None`/no-op translation above.** Anything else (permission errors, network errors) propagates naturally out of `EventStore` and up through `handlers.py` — `main.py`'s existing generic `except Exception as exc: return _failure_response(...)` already acks 200 and logs ERROR for any handler failure, so no new try/except is added anywhere else.

### 2. `handlers.py` changes

- Add `from event_store import EventStore` import.
- `handle_facility_booking(action, payload, calendar_client, event_store)` — new `event_store: EventStore` parameter. The literal string `"FacilityBooking"` is passed as `context` on every store call (it's this function's own identity, not something that needs threading through from `main.py`).
- **Create**: after `event = calendar_client.create_event(...)`, replace the TODO with:
  `event_store.set("FacilityBooking", reservation_id, event["id"], action="Create", end_iso=payload["End"])`
- **Update**: replace `event_id = _lookup_event_id(reservation_id)` with `event_id = event_store.get("FacilityBooking", reservation_id)`. Keep the existing `if event_id is None: skipped` guard. After `calendar_client.update_event(...)` succeeds, add:
  `event_store.set("FacilityBooking", reservation_id, event_id, action="Update", end_iso=payload["End"])`
- **Delete**: replace `event_id = _lookup_event_id(deleted_id)` with `event_id = event_store.get("FacilityBooking", deleted_id)`. Keep the `skipped` guard. After `calendar_client.delete_event(event_id)`, replace the TODO with:
  `event_store.delete("FacilityBooking", deleted_id)`
- **Delete `handle_registration` entirely**, along with its module-level "Registration" section comment block and the `_lookup_event_id` stub function (now fully replaced by `EventStore.get`).

### 3. `main.py` changes

- `from handlers import handle_facility_booking` (drop `handle_registration`).
- `from event_store import EventStore` import.
- `HANDLERS = {"FacilityBooking": handle_facility_booking}` — dropping the `"Registration"` entry means a `Registration` webhook now falls straight into the existing `handler is None` branch, which already returns `_respond("ignored", 200, ...)`. This already satisfies "ignored events return 200" — no new code needed, just removing the routing entry.
- New required env var, read the same eager/hard way as `CALENDAR_ID`:
  ```python
  EVENT_STORE_BUCKET = os.environ["GOOGLE_EVENT_STORE_BUCKET"]
  ```
- New lazy singleton mirroring `_get_calendar_client()`:
  ```python
  _event_store: EventStore | None = None

  def _get_event_store() -> EventStore:
      global _event_store
      if _event_store is None:
          _event_store = EventStore(bucket_name=EVENT_STORE_BUCKET)
      return _event_store
  ```
- Update the single handler call site:
  ```python
  result = handler(action=action, payload=payload, calendar_client=_get_calendar_client(), event_store=_get_event_store())
  ```

### 4. `requirements.txt`

Add `google-cloud-storage==2.*` (same `X.*` pinning style as every other line).

### 5. `env.yaml.example`

Add a second line: `GOOGLE_EVENT_STORE_BUCKET: "your-project-id-amilia-calendar-event-mappings"`.

### 6. New file: `lifecycle.json`

As shown above in **Retention**. Checked into the repo root so the retention policy is versioned and reviewable, not just a one-off command someone ran once.

### 7. New diagnostic script: `check_storage_access.py`

Mirrors `check_access.py`'s exact shape and tone: reads `GOOGLE_EVENT_STORE_BUCKET` from env (friendly early exit if missing), builds a `storage.Client()`, prints who it authenticated as (reuse the same `_identity()`-style helper), and does a real end-to-end probe — write a test object (with a near-future `custom_time`), read it back, delete it — with a summary like `"amilia-calendar-updater access check (safe to delete)"`. Explains common failure statuses (403 permission denied → check the IAM binding; 404 bucket not found → check the bucket name/project) with the same actionable-remediation style as `check_access.py`'s `_explain()`.

### 8. Tests

**`test_handlers.py`**:
- Extend `_FakeCalendarClient` with `update_event(self, event_id, **fields)` and `delete_event(self, event_id)`, each appending to tracked call lists (mirrors the existing `create_event` fake).
- Add `_FakeEventStore`:
  ```python
  class _FakeEventStore:
      def __init__(self, initial=None):
          self.mappings = dict(initial or {})  # {(context, external_id): event_id}
          self.set_calls = []
          self.delete_calls = []

      def get(self, context, external_id):
          return self.mappings.get((context, external_id))

      def set(self, context, external_id, event_id, action, end_iso):
          self.mappings[(context, external_id)] = event_id
          self.set_calls.append((context, external_id, event_id, action, end_iso))

      def delete(self, context, external_id):
          self.mappings.pop((context, external_id), None)
          self.delete_calls.append((context, external_id))
  ```
- New tests (hand-written-fake style, matching the existing two tests — no `unittest.mock`):
  - `test_facility_booking_create_stores_mapping`
  - `test_facility_booking_update_uses_stored_mapping`
  - `test_facility_booking_update_with_no_mapping_skips` (asserts no `update_event` call recorded)
  - `test_facility_booking_delete_removes_mapping_and_calendar_event`
  - `test_facility_booking_delete_with_no_mapping_skips` (asserts no `delete_event` call recorded)

**`test_main.py`**:
- Add `os.environ.setdefault("GOOGLE_EVENT_STORE_BUCKET", "test-bucket")` next to the existing `GOOGLE_CALENDAR_ID` line, before `import main`.
- Extend the `no_real_credentials` fixture to also stub the store getter: `monkeypatch.setattr(main, "_get_event_store", lambda: object())`.
- Add a test asserting `Context: "Registration"` now returns `status == "ignored"` and `status_code == 200`.

**New `test_event_store.py`** — unit-tests `EventStore` itself via an injected fake `storage.Client`-shaped object (fake client → fake bucket → fake blob supporting `upload_from_string`/`download_as_text`/`delete`/`reload`/`.custom_time`, with `download_as_text`/`reload` raising a `NotFound` stand-in when absent). Covers: `set` then `get` round-trips the event ID; `get` on a missing key returns `None`; `delete` on a missing key doesn't raise; `set` called twice with an earlier `end_iso` the second time does **not** move `custom_time` backward.

### 9. Deployment docs

- `main.py`'s deploy docstring: add a line noting the bucket must exist, be IAM-bound, and have the lifecycle policy applied before deploying (mirroring how it already says to enable the Calendar API and create the service account first), pointing at `event_store.py`'s docstring for the exact commands.
- `README.md`: add a "### Storage bucket setup" subsection under `## Deployment`, with the real `gcloud storage` commands filled in for `cm-calendar-506017` / `us-west1`, matching the README's existing style of using real values.

## GCP setup (run once, against the real project)

```bash
gcloud storage buckets create gs://cm-calendar-506017-amilia-calendar-event-mappings \
  --location=us-west1 --uniform-bucket-level-access

gcloud storage buckets add-iam-policy-binding \
  gs://cm-calendar-506017-amilia-calendar-event-mappings \
  --member="serviceAccount:amilia-calendar-updater@cm-calendar-506017.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"

gcloud storage buckets update gs://cm-calendar-506017-amilia-calendar-event-mappings \
  --lifecycle-file=lifecycle.json
```

## Verification

1. `pip install -r requirements.txt -r requirements-dev.txt && pytest` — all tests pass (existing 10 + new ones), no network calls (fakes throughout).
2. Run the three `gcloud storage` commands above against the real project.
3. `GOOGLE_EVENT_STORE_BUCKET=cm-calendar-506017-amilia-calendar-event-mappings .venv/bin/python check_storage_access.py`, first as yourself, then impersonating the service account (same `gcloud auth application-default login --impersonate-service-account=...` flow already used for `check_access.py`) — confirms the IAM binding actually works before real traffic hits it.
4. Add `GOOGLE_EVENT_STORE_BUCKET` to the real `env.yaml` and redeploy.
5. End-to-end: send a simulated `FacilityBooking` Create, then Update, then Delete (via curl or a real Amilia test booking) and confirm via Cloud Logging that Update finds the stored mapping (not `skipped: no stored event mapping`) and Delete removes both the calendar event and the GCS object (`gcloud storage cat gs://<bucket>/FacilityBooking/<id>.json` returns "not found" afterward).
6. Confirm the object's Custom-Time via `gcloud storage objects describe gs://<bucket>/FacilityBooking/<id>.json --format='value(customTime)'` matches the booking's `End` time.
7. Send a simulated `Registration` webhook and confirm it now returns `status=ignored` at HTTP 200.
8. Retention rule is verified structurally (config review + the `test_event_store.py` never-decreases-Custom-Time test) rather than by waiting 90 days — GCS's daily lifecycle sweep isn't something to block on in a manual test pass.

## Critical files

- `event_store.py` (new)
- `lifecycle.json` (new)
- `handlers.py`
- `main.py`
- `test_handlers.py`
- `test_main.py`
- `test_event_store.py` (new)
- `check_storage_access.py` (new)
- `requirements.txt`
- `env.yaml.example`
- `README.md`
