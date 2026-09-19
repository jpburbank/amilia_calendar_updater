"""
Thin wrapper around Cloud Tasks, used to fan out a Program visibility flip
to one small async task per affected Activity, instead of looping over
every child inline inside the Program webhook handler. A Program's
activity list can realistically run into the low hundreds; processing that
many Calendar/GCS operations synchronously risks the webhook response
taking long enough that Amilia (or our own Cloud Function timeout) gives
up and retries mid-fan-out, racing overlapping work against itself.

Each task carries only an Activity ID — the reconciliation worker always
re-reads that activity's current Program state at execution time rather
than trusting whatever was true when the task was enqueued, so a second
Program flip racing the first is never a correctness problem.

Setup: the queue, its IAM enqueuer grant to this function's runtime service
account, and the invoker grant letting Cloud Tasks call the
reconcile-worker Cloud Function are all Terraform-managed in the sibling
amilia_calendar_updater_gcp_resources project (tasks.tf) — run
`terraform apply` there, and deploy reconcile_worker.py as its own Cloud
Function, before relying on this.
"""

from __future__ import annotations

import json

from google.cloud import tasks_v2


class ReconcileQueue:
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

    def enqueue_reconciliation(self, activity_id) -> None:
        """Schedules a single reconcile_worker.py invocation for this activity."""
        task = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": self._worker_url,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"activity_id": activity_id}).encode(),
                "oidc_token": {"service_account_email": self._invoker_service_account},
            }
        }
        self._client.create_task(parent=self._queue_path, task=task)
