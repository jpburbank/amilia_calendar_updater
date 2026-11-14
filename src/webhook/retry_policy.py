"""
Decides whether a failed webhook task is worth retrying. Transient means
"the same request may well succeed later": network errors, timeouts,
rate limits, and 5xx from Amilia, Google Calendar, Cloud Storage, or Cloud
Tasks. Everything else (a 4xx such as the calendar not being shared with
us, bad credentials, a malformed payload, a bug) fails the same way every
time, so the worker discards it at once, logging the full webhook, rather
than retrying it for 24 hours.
"""

from __future__ import annotations

import json

import httplib2
import requests
from google.api_core import exceptions as api_core_exceptions
from googleapiclient.errors import HttpError

# Google Calendar reports rate limiting as 403 with one of these reasons.
_RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, (TimeoutError, ConnectionError, httplib2.HttpLib2Error)):
        return True
    if isinstance(exc, api_core_exceptions.RetryError):
        return True
    if isinstance(exc, requests.HTTPError):
        return _is_transient_status(getattr(exc.response, "status_code", None))
    if isinstance(exc, HttpError):
        return _is_transient_status(exc.resp.status) or _is_calendar_rate_limit(exc)
    if isinstance(exc, api_core_exceptions.GoogleAPICallError):
        return _is_transient_status(exc.code)
    return False


def _is_transient_status(status) -> bool:
    if status is None:
        return False
    status = int(status)
    return status in (408, 429) or status >= 500


def _is_calendar_rate_limit(exc: HttpError) -> bool:
    """Reads the reasons from the raw error body, e.g. {"error": {"errors": [{"reason": ...}]}}."""
    try:
        errors = json.loads(exc.content)["error"]["errors"]
    except (ValueError, KeyError, TypeError):
        return False
    return any(isinstance(e, dict) and e.get("reason") in _RATE_LIMIT_REASONS for e in errors)
