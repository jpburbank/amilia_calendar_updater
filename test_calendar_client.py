import httplib2
import pytest
from googleapiclient.errors import HttpError

from calendar_client import is_retryable


def _http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b"{}")


@pytest.mark.parametrize("status", [403, 404])
def test_calendar_sharing_problems_are_retryable(status):
    """An unshared or revoked calendar is operator-fixable within the retry window."""
    assert is_retryable(_http_error(status)) is True


@pytest.mark.parametrize("status", [429, 500, 503])
def test_google_side_failures_are_retryable(status):
    assert is_retryable(_http_error(status)) is True


@pytest.mark.parametrize("status", [400, 401, 409, 412])
def test_malformed_requests_are_not_retryable(status):
    assert is_retryable(_http_error(status)) is False


def test_transport_failures_are_retryable():
    assert is_retryable(ConnectionError("connection reset by peer")) is True


def test_missing_payload_field_is_not_retryable():
    assert is_retryable(KeyError("Start")) is False
