"""Tests for WebhookQueue, against a fake Cloud Tasks client."""

import json
from datetime import UTC, datetime

from webhook_queue import WebhookQueue


class _FakeTasksClient:
    def __init__(self):
        self.created = []

    def queue_path(self, project, location, queue):
        return f"projects/{project}/locations/{location}/queues/{queue}"

    def create_task(self, parent, task):
        self.created.append((parent, task))


def test_enqueue_webhook_posts_the_raw_webhook_to_the_worker_with_oidc():
    client = _FakeTasksClient()
    queue = WebhookQueue(
        project_id="p",
        location="us-west1",
        queue_id="amilia-webhook-processing",
        worker_url="https://example.invalid/worker",
        invoker_service_account="sa@p.iam.gserviceaccount.com",
        client=client,
    )
    webhook = {"Context": "Activity", "Action": "Update", "Payload": {"Id": 1}}

    queue.enqueue_webhook(webhook, now=datetime(2026, 11, 14, 12, 0, tzinfo=UTC))

    parent, task = client.created[0]
    assert parent == "projects/p/locations/us-west1/queues/amilia-webhook-processing"
    request = task["http_request"]
    assert request["url"] == "https://example.invalid/worker"
    assert request["oidc_token"] == {"service_account_email": "sa@p.iam.gserviceaccount.com"}
    assert json.loads(request["body"]) == {
        "webhook": webhook,
        "enqueued_at": "2026-11-14T12:00:00+00:00",
    }
