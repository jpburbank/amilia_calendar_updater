"""Tests for AmiliaClient, against a fake requests.Session-shaped object."""

import json
import logging

import pytest

from amilia_client import AUTHENTICATE_URL, AmiliaClient, AmiliaNotFound

OCCURRENCES_URL = "https://app.amilia.com/api/v3/en/org/17659/activities/1/occurrences"
ACTIVITY_URL = "https://app.amilia.com/api/v3/en/org/17659/activities/1"
PROGRAM_URL = "https://app.amilia.com/api/v3/en/org/17659/programs/107638"


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}

    def json(self):
        return self._json_data

    @property
    def text(self):
        return json.dumps(self._json_data)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    """Returns queued responses per URL, in order, one per call."""

    def __init__(self, responses: dict):
        self._responses = {url: list(queue) for url, queue in responses.items()}
        self.calls = []

    def get(self, url, headers=None, auth=None, params=None):
        self.calls.append({"url": url, "headers": headers, "auth": auth, "params": params})
        return self._responses[url].pop(0)


def _occurrences_page(items, total_count):
    return _FakeResponse(200, {"Items": items, "Paging": {"TotalCount": total_count, "Next": ""}})


def test_authenticates_before_first_call():
    session = _FakeSession(
        {
            AUTHENTICATE_URL: [_FakeResponse(200, {"Token": "tok1"})],
            OCCURRENCES_URL: [_occurrences_page([{"Id": 1}], 1)],
        }
    )
    client = AmiliaClient(username="u", password="p", session=session)

    result = client.get_activity_occurrences(17659, 1)

    assert result == [{"Id": 1}]
    auth_call = next(c for c in session.calls if c["url"] == AUTHENTICATE_URL)
    assert auth_call["auth"] == ("u", "p")
    occ_call = next(c for c in session.calls if c["url"] == OCCURRENCES_URL)
    assert occ_call["headers"]["Authorization"] == "Bearer tok1"


def test_reuses_cached_token_across_calls():
    session = _FakeSession(
        {
            AUTHENTICATE_URL: [_FakeResponse(200, {"Token": "tok1"})],
            OCCURRENCES_URL: [
                _occurrences_page([{"Id": 1}], 1),
                _occurrences_page([{"Id": 1}], 1),
            ],
        }
    )
    client = AmiliaClient(username="u", password="p", session=session)

    client.get_activity_occurrences(17659, 1)
    client.get_activity_occurrences(17659, 1)

    assert len([c for c in session.calls if c["url"] == AUTHENTICATE_URL]) == 1


def test_reauthenticates_once_on_401():
    session = _FakeSession(
        {
            AUTHENTICATE_URL: [
                _FakeResponse(200, {"Token": "tok1"}),
                _FakeResponse(200, {"Token": "tok2"}),
            ],
            OCCURRENCES_URL: [
                _FakeResponse(401),
                _occurrences_page([{"Id": 1}], 1),
            ],
        }
    )
    client = AmiliaClient(username="u", password="p", session=session)

    result = client.get_activity_occurrences(17659, 1)

    assert result == [{"Id": 1}]
    occ_calls = [c for c in session.calls if c["url"] == OCCURRENCES_URL]
    assert occ_calls[0]["headers"]["Authorization"] == "Bearer tok1"
    assert occ_calls[1]["headers"]["Authorization"] == "Bearer tok2"


def test_follows_pagination_until_total_count_reached():
    session = _FakeSession(
        {
            AUTHENTICATE_URL: [_FakeResponse(200, {"Token": "tok1"})],
            OCCURRENCES_URL: [
                _occurrences_page([{"Id": 1}, {"Id": 2}], 3),
                _occurrences_page([{"Id": 3}], 3),
            ],
        }
    )
    client = AmiliaClient(username="u", password="p", session=session)

    result = client.get_activity_occurrences(17659, 1)

    assert [item["Id"] for item in result] == [1, 2, 3]
    occ_calls = [c for c in session.calls if c["url"] == OCCURRENCES_URL]
    assert occ_calls[0]["params"]["page"] == 1
    assert occ_calls[1]["params"]["page"] == 2


def test_get_activity_returns_current_activity():
    session = _FakeSession(
        {
            AUTHENTICATE_URL: [_FakeResponse(200, {"Token": "tok1"})],
            ACTIVITY_URL: [_FakeResponse(200, {"Id": 1, "Status": "Hidden"})],
        }
    )
    client = AmiliaClient(username="u", password="p", session=session)

    assert client.get_activity(17659, 1) == {"Id": 1, "Status": "Hidden"}


def test_get_program_returns_current_program():
    session = _FakeSession(
        {
            AUTHENTICATE_URL: [_FakeResponse(200, {"Token": "tok1"})],
            PROGRAM_URL: [_FakeResponse(200, {"Id": 107638, "IsArchived": True})],
        }
    )
    client = AmiliaClient(username="u", password="p", session=session)

    assert client.get_program(17659, 107638)["IsArchived"] is True


@pytest.mark.parametrize(
    "url, call",
    [
        (ACTIVITY_URL, lambda c: c.get_activity(17659, 1)),
        (PROGRAM_URL, lambda c: c.get_program(17659, 107638)),
        (OCCURRENCES_URL, lambda c: c.get_activity_occurrences(17659, 1)),
    ],
)
def test_404_raises_amilia_not_found(url, call):
    session = _FakeSession(
        {
            AUTHENTICATE_URL: [_FakeResponse(200, {"Token": "tok1"})],
            url: [_FakeResponse(404, {"Message": "Entity was not found"})],
        }
    )
    client = AmiliaClient(username="u", password="p", session=session)

    with pytest.raises(AmiliaNotFound):
        call(client)


def test_other_errors_still_raise_http_errors():
    session = _FakeSession(
        {
            AUTHENTICATE_URL: [_FakeResponse(200, {"Token": "tok1"})],
            ACTIVITY_URL: [_FakeResponse(503)],
        }
    )
    client = AmiliaClient(username="u", password="p", session=session)

    with pytest.raises(RuntimeError, match="HTTP 503"):
        client.get_activity(17659, 1)


def _rest_response_lines(caplog):
    return [r.getMessage() for r in caplog.records if "AMILIA_REST_RESPONSE" in r.getMessage()]


def test_logs_every_rest_response_body_verbatim(caplog):
    page = {
        "Items": [{"Id": 1, "Start": "s", "Location": {"Name": "Shop"}}],
        "Paging": {"TotalCount": 1},
    }
    session = _FakeSession(
        {
            AUTHENTICATE_URL: [_FakeResponse(200, {"Token": "tok1"})],
            OCCURRENCES_URL: [_FakeResponse(200, page)],
        }
    )
    client = AmiliaClient(username="u", password="p", session=session)

    with caplog.at_level(logging.INFO):
        client.get_activity_occurrences(17659, 1)

    assert any(line.endswith(f"body={json.dumps(page)}") for line in _rest_response_lines(caplog))


def test_logs_error_response_body_before_raising(caplog):
    session = _FakeSession(
        {
            AUTHENTICATE_URL: [_FakeResponse(200, {"Token": "tok1"})],
            OCCURRENCES_URL: [_FakeResponse(503, {"Message": "unavailable"})],
        }
    )
    client = AmiliaClient(username="u", password="p", session=session)

    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError):
        client.get_activity_occurrences(17659, 1)

    assert any(
        "status=503" in line and "unavailable" in line for line in _rest_response_lines(caplog)
    )


def test_never_logs_the_bearer_token(caplog):
    session = _FakeSession(
        {
            AUTHENTICATE_URL: [_FakeResponse(200, {"Token": "secret-token"})],
            OCCURRENCES_URL: [_occurrences_page([{"Id": 1}], 1)],
        }
    )
    client = AmiliaClient(username="u", password="p", session=session)

    with caplog.at_level(logging.INFO):
        client.get_activity_occurrences(17659, 1)

    assert "secret-token" not in caplog.text


def test_splits_large_response_bodies_across_log_entries(caplog, monkeypatch):
    monkeypatch.setattr("amilia_client._LOG_CHUNK_CHARS", 10)
    page = {"Items": [{"Id": 1, "Name": "x" * 40}], "Paging": {"TotalCount": 1}}
    session = _FakeSession(
        {
            AUTHENTICATE_URL: [_FakeResponse(200, {"Token": "tok1"})],
            OCCURRENCES_URL: [_FakeResponse(200, page)],
        }
    )
    client = AmiliaClient(username="u", password="p", session=session)

    with caplog.at_level(logging.INFO):
        client.get_activity_occurrences(17659, 1)

    reassembled = "".join(line.split(" body=", 1)[1] for line in _rest_response_lines(caplog))
    assert reassembled == json.dumps(page)
