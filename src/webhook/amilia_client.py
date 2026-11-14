"""
Thin wrapper around Amilia's v3 org REST API. Used for:
  - an Activity's real per-occurrence schedule (Start/End/Location per
    session), which Amilia's webhooks never include — only a
    human-readable ScheduleSummary string, not structured data;
  - an Activity's or Program's *current* state, so a replayed old webhook
    can't resurrect something since hidden, archived, or deleted.
    Verified live 2026-11-13: a deleted Activity is 404; an archived
    Activity is 200 with Status "Hidden" (no archived flag); an archived
    Program is 200 with IsArchived true.

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

import logging
import uuid

import requests

logger = logging.getLogger(__name__)

# TEMP DEBUG — remove before merging. Cloud Logging rejects entries over
# 256 KB, so a large response body is split across several entries
# (part=i/n, same request_id) rather than risk losing it.
_LOG_CHUNK_CHARS = 100_000


class AmiliaNotFound(Exception):
    """Amilia answered 404: the record no longer exists (deleted)."""


AUTHENTICATE_URL = "https://www.amilia.com/api/V3/authenticate"
API_BASE_URL = "https://app.amilia.com/api/v3/en"


class AmiliaClient:
    def __init__(self, username: str, password: str, session=None):
        self._username = username
        self._password = password
        self._session = session or requests.Session()
        self._token: str | None = None

    def get_activity(self, org_id, activity_id) -> dict:
        """The Activity as Amilia has it now. Raises AmiliaNotFound if deleted."""
        return self._get(f"{API_BASE_URL}/org/{org_id}/activities/{activity_id}").json()

    def get_program(self, org_id, program_id) -> dict:
        """The Program as Amilia has it now. Raises AmiliaNotFound if deleted."""
        return self._get(f"{API_BASE_URL}/org/{org_id}/programs/{program_id}").json()

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
        _log_response(url, kwargs.get("params"), response)
        if response.status_code == 401:
            self._token = None
            response = self._session.get(url, headers=self._auth_header(), **kwargs)
            _log_response(url, kwargs.get("params"), response)
        if response.status_code == 404:
            raise AmiliaNotFound(url)
        response.raise_for_status()
        return response

    def _auth_header(self) -> dict:
        return {"Authorization": f"Bearer {self._token or self._authenticate()}"}

    def _authenticate(self) -> str:
        response = self._session.get(AUTHENTICATE_URL, auth=(self._username, self._password))
        # TEMP DEBUG — status only: the body is the bearer token.
        logger.info("AMILIA_REST_AUTH status=%s", response.status_code)
        response.raise_for_status()
        self._token = response.json()["Token"]
        return self._token


def _log_response(url: str, params, response) -> None:
    """
    TEMP DEBUG — remove before merging. Logs every Amilia REST response
    verbatim (success or error, before raise_for_status can discard it).
    Filter with: textPayload:"AMILIA_REST_RESPONSE"
    """
    body = response.text
    chunks = [body[i : i + _LOG_CHUNK_CHARS] for i in range(0, len(body), _LOG_CHUNK_CHARS)] or [""]
    request_id = uuid.uuid4().hex[:8]
    for index, chunk in enumerate(chunks, start=1):
        logger.info(
            "AMILIA_REST_RESPONSE request_id=%s part=%d/%d url=%s params=%s status=%s body=%s",
            request_id,
            index,
            len(chunks),
            url,
            params,
            response.status_code,
            chunk,
        )
