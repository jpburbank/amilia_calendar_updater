# Open security issues

Known security concerns that have been identified but not yet resolved. Each
entry records what was found, the evidence, the risk, and the agreed or
proposed fix, so nothing is lost while it waits to be scheduled. Remove an
entry (or move it to a "Resolved" section with the date) once it's fixed.

---

## 1. Webhook token exposed through Slack link previews

**Found:** 2026-11-10. **Status:** open, token not yet rotated.

### What was found

Amilia has no webhook signing, so `amilia_webhook` authenticates callers with
a shared secret passed as a `?token=` query parameter (see
`src/webhook/main.py`). That makes the full webhook URL itself the credential.

Cloud Run request logs show the full tokenized URL being fetched by
`Slackbot-LinkExpanding 1.0`, the crawler Slack uses to build link previews
("unfurls") whenever a message containing a URL is posted or edited:

- 199 GET requests between 2026-10-13 and 2026-11-10 (the start of the window
  is likely just the 30-day log retention limit, not when it began).
- Every one carried the **current, valid** token: one token value across all
  of them, matching the only version of the `amilia-webhook-token` secret
  (created 2026-09-15).
- They came from 187 different AWS IP addresses (Slack's crawler pool).
- Almost all fell between 15:00 and 02:00 UTC, i.e. Pacific business hours,
  with bursts of up to 7 in one minute. That pattern fits people posting,
  replying with, or editing messages containing the URL, more than a single
  old message.
- Every request got a 405, because the token check passes and then the
  function rejects non-POST methods. So the previews did nothing harmful by
  themselves.

Genuine Amilia deliveries all arrive as POSTs from `64.187.182.76`.

### Risk

Anyone who can read that Slack message, including channel members, future
members, anyone it's shared or forwarded to, and Slack search or exports, has
a working credential. With it they can POST forged Program or Activity
payloads (and FacilityBooking payloads while that handler is enabled) and
create, change, or delete calendar events. The token also lives on in
Slack's history after rotation, but rotating makes the old value useless.

### Fix

1. Find where the URL is being posted in Slack. Slack search for
   `cloudfunctions.net/amilia-calendar-updater` should find it. Delete or edit
   those messages, and check for an integration or bot that posts it
   automatically.
2. Rotate the token: add a new version of the `amilia-webhook-token` secret,
   redeploy `amilia-calendar-updater` (it reads `:latest` at deploy time),
   update the URL on every Amilia webhook subscription, then disable the old
   secret version.
3. When sharing the URL in future, share it without the `?token=` part.
4. Optional hardening: reject requests whose source IP isn't Amilia's (needs
   Amilia to confirm their egress IPs are stable), and/or alert on any
   `status=rejected` request that carried a valid token.

---

## 2. Public GitHub repository

**Found:** 2026-11-10. **Status:** open, to be addressed later.

`github.com/jpburbank/amilia_calendar_updater` is public. No secrets are
committed (tokens and passwords are in Secret Manager, and `env.yaml` is
gitignored), but the repo does publish operational details an attacker would
otherwise have to discover:

- the GCP project ID (`cm-calendar-506017`) and region (`us-west1`), and so
  the predictable Cloud Functions URLs;
- the runtime service-account email;
- the event-store bucket name;
- the Amilia organization ID (17659);
- the complete request-handling logic, including exactly what a forged
  webhook would need to contain.

The sibling Terraform repo, `github.com/jpburbank/amilia_calendar_updater_gcp_resources`, is
**also public**. It publishes the full IAM layout, secret names, and Cloud Tasks queue config.
Its `terraform.tfstate`, which holds the generated webhook token in plain text, is gitignored
and was confirmed untracked on 2026-11-10. Keep it that way, or move state to a private GCS
backend.

None of these are credentials by themselves. Together with the leaked token
above, they were everything needed to forge webhooks. Also watch for
accidental commits of local scratch files (see the `tmp_*.py` rule in
`.gitignore`) and of calendar IDs once the production calendar ID moves into
Secret Manager.

### Options to decide on later

- Make both repositories private (simplest; nothing in this project needs to
  be public).
- Or keep it public and scrub environment-specific values from docs and
  defaults in favor of placeholders, accepting that git history still holds
  them.
