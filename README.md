# Amilia Calendar Updater

Keeps a Google Calendar in sync with Create Makerspace's classes in Amilia. When an Activity is
created, changed or deleted in Amilia, its calendar events follow within seconds.

Open security issues are tracked in [`docs/security-notes.md`](docs/security-notes.md).


## How it works

```
Amilia webhook ──► amilia-calendar-updater ──► queue ──► amilia-webhook-worker ──► Google Calendar
                   (public: checks token,                (private: does the sync)    + event store
                    queues, replies in ms)                       │
                                                                 └─► amilia-activity-reconciler
                                                                     (when a Program goes online/offline)
```

- **Three Cloud Functions.** `amilia-calendar-updater` receives Amilia's webhooks,
  `amilia-webhook-worker` processes them, and `amilia-activity-reconciler` shows or hides a
  Program's Activities when the Program's `Online` flag changes.
- **Amilia's REST API** fills in what webhooks don't carry: each occurrence's times and room, and
  each record's current state.
- **The event store** (a Cloud Storage bucket) remembers which calendar event belongs to which
  Amilia occurrence.


## What gets synced

Amilia **Programs**, **Activities**, and each Activity's **occurrences**. Each occurrence (session)
is one calendar event.

**FacilityBookings are off by default.** The calendar no longer needs them (since 2026-11), but
the code is kept and tested behind a flag in `env.yaml`:

| `FACILITY_BOOKING_SYNC_ENABLED` | Effect |
|---|---|
| `"false"` (default) | FacilityBooking webhooks are answered `status=ignored`; the backfill skips reservations |
| `"true"` | FacilityBooking webhooks and reservations are synced |

Any other value stops the function at startup. A change takes effect on the next deploy. Turning
it off leaves existing FacilityBooking events and records in place, no longer updated. See
`src/shared/feature_flags.py`.


## What goes into a calendar event

| Event field | Comes from |
|---|---|
| Title | The Activity's `Name`, trimmed of stray spaces (`Activity` if blank) |
| Start / end | The occurrence's `Start` / `End` from Amilia's REST API |
| Location | The occurrence's room, then the building address: `Wood shop, 39449 Queens Way #1, Squamish, BC V8B 0R5` |
| Colour | Picked from the room (e.g. Pottery Studio → Tangerine); an unlisted room gets the default |
| Description | A **Book Now** link, then Description, Note, Prerequisite, Additional Information, Contact person |
| Hidden tags | `amilia_type` and `amilia_type_id`, identifying the Amilia record |

**To change the building address or the room colours, edit `src/shared/constants.py`.**

- **Room.** The occurrence's `Location.Name`, falling back to the Activity's
  `Schedules[].Locations[].Name`. Only the room is stored; the address is added when the event is
  written. See `src/shared/locations.py`.
- **Book Now.** Links to the Activity's `Url`, or its `SecretUrl` if `Url` isn't a valid http(s)
  URL. No link if neither is valid.
- **Description sections.** Each is its own paragraph, with a bold heading except Description
  itself. Empty ones are left out.
- **Hidden tags.** Stored in the event's `extendedProperties.private`. They never show in the
  Calendar UI, but anyone with API read access to the event can read them.

The webhook worker, the reconciler and the backfill all build events with the same code
(`src/shared/activities.py`), so they always agree.

**Existing events** pick up changes to any of the above on their Activity's next Update, or when
the backfill runs with `--update-existing`.


## How sync stays correct

**Stale webhooks are skipped.** Amilia can redeliver an old webhook minutes later, after newer
ones. Each stored record keeps the `EventTime` of the webhook that last wrote it; a webhook that
isn't newer is answered `status=skipped reason=stale event` and changes nothing. Records without
one (e.g. written by the backfill) accept any webhook.

**Amilia is asked for the current state.** Activity and Program Create/Update webhooks are
checked against Amilia's REST API:

| Amilia says | Result |
|---|---|
| Not found (deleted) | `status=skipped reason=deleted in Amilia` |
| Program archived | `status=skipped reason=archived in Amilia` |
| Activity's current `Status` | Used instead of the webhook's (archived Activities show as `Hidden`) |
| Lookup fails (timeout, 5xx) | Retried; on the final attempt the webhook's own data is used, so an outage doesn't lose changes |

**Activity dates come from REST too.** The webhook's `StartDate`/`EndDate` have the wrong UTC
offset, so stored dates use the REST Activity's. Event times were never affected.

**Retries never duplicate events.** Each event's ID is derived from its Amilia IDs
(`stable_event_id` in `src/shared/calendar_client.py`). A repeated create updates the existing
event instead, restoring it if it was deleted.


## Background webhook processing

`amilia-calendar-updater` checks the token, puts the webhook on the `amilia-webhook-processing`
queue and replies `status=queued` in milliseconds; slow replies (2.7–5.9 s, seen 2026-11-13) made
Amilia redeliver. `amilia-webhook-worker`, called only by Cloud Tasks, does the actual sync.

| Worker result | What happens next |
|---|---|
| `ok` / `skipped` / `ignored` | Done |
| `retrying`: a temporary failure (network, timeout, 429/5xx, rate limit) | Retried with backoff from 10 s, doubling, up to 10 min apart |
| `discarded`: a permanent failure, or 24 h of retries | Given up. **One ERROR log entry holds the full webhook, the error and the traceback**, so the change can be redone by hand |

- **One task at a time.** The queue runs one task at a time, so two changes to the same record
  can't race. Volume is a few dozen webhooks a day.
- **Timing.** The worker gives up after `WEBHOOK_TASK_GIVE_UP_AFTER_SECONDS` (24 h). The queue's
  own limit (Terraform `webhook_task_max_retry_duration`, 25 h) is a backstop and must stay longer.
- **If queueing fails**, the webhook is processed inline instead (`status=ok` or `dropped`).
- **Retry rules** are in `src/webhook/retry_policy.py`.

Find given-up webhooks in Cloud Logging with `jsonPayload.message:"status=discarded"` (or
`textPayload:`). Emailing them is [issue #26](https://github.com/jpburbank/amilia_calendar_updater/issues/26).


## More documentation

- **[Operations](docs/operations.md)**: setup, secrets, Terraform, deploying, backfill, bulk
  delete, switching calendars.
- **[Development](docs/development.md)**: Python environment, lint and tests, source layout.
- **[Everything in docs/](docs/README.md)**, including the security notes.
