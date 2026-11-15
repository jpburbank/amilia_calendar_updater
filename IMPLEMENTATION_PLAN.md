# Share code between the backfill and the Cloud Functions

Stacked on PR #29 (Activity dates from REST).

## Stage 1: Shared Amilia client
**Goal**: One Amilia REST client in `src/shared/amilia_client.py`, used by the webhook and the backfill.
**Success Criteria**: `_AmiliaRestClient` is gone; backfill gets 404 → `AmiliaNotFound` and the same logging.
**Tests**: Existing client tests, plus the backfill's page-size/date-range tests moved to test_amilia_client.py.
**Status**: Complete

## Stage 2: Shared Activity event fields and record
**Goal**: `src/shared/activities.py` with the description builder, the calendar event fields, and the stored record builder, used by the handler, backfill and reconciler. The record stores the description.
**Success Criteria**: Reconciler-created events carry the description and Amilia ID tags.
**Tests**: Builder unit tests; reconciler test for description/extended properties.
**Status**: Complete

## Stage 3: Backfill `--update-existing`
**Goal**: Patch-only mode that updates existing events' fields; never deletes. Dry run prints only the fields that would change.
**Success Criteria**: Default behaviour unchanged; `--update-existing` patches; `--dry-run --update-existing` reads but writes nothing.
**Tests**: Backfill tests for each mode.
**Status**: Complete

## Stage 4: Docs
**Goal**: README and docstrings describe the shared modules and the new flag.
**Status**: In Progress
