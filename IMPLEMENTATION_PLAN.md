# Background webhook processing

Infra: amilia_calendar_updater_gcp_resources PR #6 (queue, enqueuer and invoker grants).
Email on discard: deferred to issue #26.

## Stage 1: Retry-safe calendar event creation
**Goal**: Creating an event twice (a retried task) never duplicates it.
**Success Criteria**: `create_event` takes a deterministic `event_id`. A 409 (the ID already
exists, live or cancelled) patches that event and sets `status: confirmed`, which also restores
a previously deleted one. Handlers, the reconciler, and the backfill all pass IDs derived from
Amilia IDs.
**Tests**: ID is valid base32hex and stable; 409 falls back to patch with `status: confirmed`;
other HttpErrors propagate; each caller passes the expected ID.
**Status**: Complete

## Stage 2: Fail open only on the final attempt
**Goal**: While retries remain, a failed current-state lookup retries instead of falling back to
the webhook payload.
**Success Criteria**: Handlers take `fail_open`. When False, a non-404 lookup error propagates;
when True (default, and the final attempt), the current fallback applies.
**Tests**: Activity and Program lookup errors propagate with `fail_open=False` and fall back
with `fail_open=True`.
**Status**: Complete

## Stage 3: Enqueue in the webhook, process in a private worker
**Goal**: The public webhook only checks the token and enqueues the webhook. A new entry point,
`process_webhook_task`, deployed as the private `amilia-webhook-worker`, runs the handlers.
**Success Criteria**:
- The webhook answers `queued` (200). Contexts with no handler are still answered `ignored`
  without enqueueing. If the enqueue fails, it processes inline as before (fail open).
- The worker: ok/skipped → 200. A transient error before the give-up time → 503, so Cloud Tasks
  retries. A permanent error, or any error at or after `WEBHOOK_TASK_GIVE_UP_AFTER_SECONDS`
  (24h) since enqueue → one ERROR `status=discarded` log entry holding the full webhook body and
  the error, then 200.
**Tests**: queued / ignored / enqueue-failure fallback; worker ok, skipped, transient → 503,
permanent → discarded, final attempt → fail_open plus discarded; transient classification.
**Status**: In Progress

## Stage 4: Docs and deploy
**Goal**: README, env.yaml.example, and the deploy commands cover the worker, the new env vars,
and the rollout order.
**Success Criteria**: A fresh reader can deploy all three functions from the README.
**Tests**: n/a (docs)
**Status**: Not Started
