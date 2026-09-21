"""
Thin wrapper around Amilia's v3 org REST API. Used for exactly one thing:
fetching an Activity's real per-occurrence schedule (Start/End/Location per
session), which Amilia's webhooks never include — only a human-readable
ScheduleSummary string, not structured data. Everything else this app needs
(Program's Online flag, Activity's Status/ProgramId/etc.) is already in the
webhook payloads, so no other REST calls are made from the webhook handlers.

Auth model: a dedicated Amilia user account, not the runtime service
account (Amilia has no concept of a GCP identity). Amilia issues a JWT
bearer token valid for about a year via HTTP Basic auth against a
dedicated authenticate endpoint; this class caches that token in memory
for the life of the instance (a fresh one is fetched on the first call,
and again automatically if a request ever comes back 401).

Setup: create a dedicated Amilia user for this integration (not tied to a
specific staff member's own login), then store its username and password
in Secret Manager. Terraform (amilia_calendar_updater_gcp_resources,
secrets.tf) creates the amilia-api-username / amilia-api-password secret
*containers* only — `terraform apply` cannot set real Amilia credentials as
values, since nothing there should ever see or generate a real password.
Set them manually after creating the Amilia user:
    printf '%s' '<username>' | gcloud secrets versions add amilia-api-username --data-file=-
    printf '%s' '<password>' | gcloud secrets versions add amilia-api-password --data-file=-
Both are mounted as plain env vars at deploy time via --set-secrets, same
as AMILIA_WEBHOOK_TOKEN.
"""

from __future__ import annotations

import requests

AUTHENTICATE_URL = "https://www.amilia.com/api/V3/authenticate"
API_BASE_URL = "https://app.amilia.com/api/v3/en"


class AmiliaClient:
    def __init__(self, username: str, password: str, session=None):
        self._username = username
        self._password = password
        self._session = session or requests.Session()
        self._token: str | None = None

    def get_activity_occurrences(self, org_id, activity_id) -> list[dict]:
        """Returns every occurrence (Id, Start, End, Location, ...) for an activity."""
        return self._get_all_pages(
            f"{API_BASE_URL}/org/{org_id}/activities/{activity_id}/occurrences"
        )

    def _get_all_pages(self, url: str) -> list[dict]:
        items = []
        page = 1
        while True:
            data = self._get(url, params={"page": page, "perPage": 2000}).json()
            items.extend(data["Items"])
            if len(items) >= data["Paging"]["TotalCount"]:
                return items
            page += 1

    def _get(self, url: str, **kwargs) -> requests.Response:
        response = self._session.get(url, headers=self._auth_header(), **kwargs)
        if response.status_code == 401:
            self._token = None
            response = self._session.get(url, headers=self._auth_header(), **kwargs)
        response.raise_for_status()
        return response

    def _auth_header(self) -> dict:
        return {"Authorization": f"Bearer {self._token or self._authenticate()}"}

    def _authenticate(self) -> str:
        response = self._session.get(AUTHENTICATE_URL, auth=(self._username, self._password))
        response.raise_for_status()
        self._token = response.json()["Token"]
        return self._token
