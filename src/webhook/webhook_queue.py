"""
Thin wrapper around Cloud Tasks for background webhook processing. The
public webhook function (amilia_webhook in main.py) only checks the token
and enqueues the raw Amilia webhook here; the private worker function
(process_webhook_task in main.py, deployed as amilia-webhook-worker) runs
the handlers. That keeps the reply to Amilia at milliseconds (live
2026-11-13, replies of 2.7-5.9s were followed by redeliveries) and lets a
failed Amilia/Calendar call be retried instead of dropped.

Each task records when it was enqueued, so the worker can tell when the
retry window (WEBHOOK_TASK_GIVE_UP_AFTER_SECONDS) has run out and make its
own final attempt: Cloud Tasks itself never says "this is the last try".

Setup: the queue (one task at a time, backoff 10s-10min, a 25h backstop),
the enqueuer grant, and the invoker grant on the worker function are
Terraform-managed in amilia_calendar_updater_gcp_resources (tasks.tf).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from google.cloud import tasks_v2


class WebhookQueue:
    def __init__(
        self,
        project_id: str,
        location: str,
        queue_id: str,
        worker_url: str,
        invoker_service_account: str,
        client=None,
    ):
        self._client = client or tasks_v2.CloudTasksClient()
        self._queue_path = self._client.queue_path(project_id, location, queue_id)
        self._worker_url = worker_url
        self._invoker_service_account = invoker_service_account

    def enqueue_webhook(self, webhook: dict, now: datetime | None = None) -> None:
        """Schedules one worker invocation carrying the raw webhook body."""
        enqueued_at = (now or datetime.now(UTC)).isoformat()
        task = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": self._worker_url,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"webhook": webhook, "enqueued_at": enqueued_at}).encode(),
                "oidc_token": {"service_account_email": self._invoker_service_account},
            }
        }
        self._client.create_task(parent=self._queue_path, task=task)
