"""Tests for which failed webhook tasks are retried."""

import json

import httplib2
import pytest
import requests
from google.api_core import exceptions as api_core_exceptions
from googleapiclient.errors import HttpError

from retry_policy import is_transient


def _requests_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"HTTP {status}", response=response)


def _calendar_error(status, reason=None):
    content = {
        "error": {
            "code": status,
            "message": "error",
            "errors": [{"domain": "usageLimits", "reason": reason}] if reason else [],
        }
    }
    return HttpError(httplib2.Response({"status": status}), json.dumps(content).encode())


@pytest.mark.parametrize(
    "exc",
    [
        requests.ConnectionError("reset"),
        requests.Timeout("slow"),
        TimeoutError(),
        ConnectionResetError(),
        httplib2.ServerNotFoundError("dns"),
        _requests_error(500),
        _requests_error(503),
        _requests_error(429),
        _calendar_error(500),
        _calendar_error(429),
        _calendar_error(403, "rateLimitExceeded"),
        _calendar_error(403, "userRateLimitExceeded"),
        api_core_exceptions.ServiceUnavailable("gcs"),
        api_core_exceptions.TooManyRequests("gcs"),
        api_core_exceptions.RetryError("gave up", cause=None),
    ],
)
def test_transient_failures_are_retried(exc):
    assert is_transient(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        _requests_error(400),
        _requests_error(401),
        _calendar_error(403),
        _calendar_error(403, "forbidden"),
        _calendar_error(404),
        api_core_exceptions.Forbidden("gcs"),
        HttpError(httplib2.Response({"status": 403}), b"not json"),
        KeyError("Start"),
        ValueError("bad"),
    ],
)
def test_permanent_failures_are_not_retried(exc):
    assert is_transient(exc) is False
