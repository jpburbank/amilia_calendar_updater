"""Tests for AmiliaClient, against a fake requests.Session-shaped object."""

from amilia_client import AUTHENTICATE_URL, AmiliaClient

OCCURRENCES_URL = "https://app.amilia.com/api/v3/en/org/17659/activities/1/occurrences"


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}

    def json(self):
        return self._json_data

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
