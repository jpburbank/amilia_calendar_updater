"""Tests for EventStore, against a fake storage.Client-shaped object."""

from datetime import datetime

from google.api_core.exceptions import NotFound

from shared.event_store import EventStore


class _FakeBlob:
    def __init__(self, bucket, name):
        self._bucket = bucket
        self.name = name
        self.custom_time = None

    def reload(self):
        if self.name not in self._bucket.objects:
            raise NotFound(self.name)
        self.custom_time = self._bucket.objects[self.name]["custom_time"]

    def upload_from_string(self, data, content_type=None):
        self._bucket.objects[self.name] = {"data": data, "custom_time": self.custom_time}

    def download_as_text(self):
        if self.name not in self._bucket.objects:
            raise NotFound(self.name)
        return self._bucket.objects[self.name]["data"]

    def delete(self):
        if self.name not in self._bucket.objects:
            raise NotFound(self.name)
        del self._bucket.objects[self.name]


class _FakeBucket:
    def __init__(self):
        self.objects = {}

    def blob(self, name):
        return _FakeBlob(self, name)

    def list_blobs(self, prefix):
        return [_FakeBlob(self, name) for name in self.objects if name.startswith(prefix)]


class _FakeClient:
    def __init__(self):
        self._bucket = _FakeBucket()

    def bucket(self, name):
        return self._bucket


def test_set_then_get_round_trips_fields():
    store = EventStore(bucket_name="test-bucket", client=_FakeClient())

    store.set("FacilityBooking", "FB-1", {"calendar_event_id": "evt_123", "action": "Create"})

    doc = store.get("FacilityBooking", "FB-1")
    assert doc["calendar_event_id"] == "evt_123"
    assert doc["action"] == "Create"


def test_get_on_missing_key_returns_none():
    store = EventStore(bucket_name="test-bucket", client=_FakeClient())

    assert store.get("FacilityBooking", "FB-missing") is None


def test_delete_on_missing_key_does_not_raise():
    store = EventStore(bucket_name="test-bucket", client=_FakeClient())

    store.delete("FacilityBooking", "FB-missing")  # must not raise


def test_delete_removes_the_document():
    store = EventStore(bucket_name="test-bucket", client=_FakeClient())
    store.set("FacilityBooking", "FB-1", {"calendar_event_id": "evt_123"})

    store.delete("FacilityBooking", "FB-1")

    assert store.get("FacilityBooking", "FB-1") is None


def test_set_without_custom_time_leaves_it_unset():
    store = EventStore(bucket_name="test-bucket", client=_FakeClient())

    store.set("Program", "107638", {"online": True})

    blob = store._blob("Program", "107638")
    blob.reload()
    assert blob.custom_time is None


def test_set_does_not_move_custom_time_earlier():
    store = EventStore(bucket_name="test-bucket", client=_FakeClient())
    store.set(
        "FacilityBooking", "FB-1", {"calendar_event_id": "evt_123"}, custom_time="2026-06-01T11:00:00"
    )

    store.set(
        "FacilityBooking", "FB-1", {"calendar_event_id": "evt_123"}, custom_time="2026-01-01T09:00:00"
    )

    blob = store._blob("FacilityBooking", "FB-1")
    blob.reload()
    assert blob.custom_time == datetime.fromisoformat("2026-06-01T11:00:00")


def test_add_list_and_remove_membership():
    store = EventStore(bucket_name="test-bucket", client=_FakeClient())

    store.add_membership("Program", "107638", "Activities", "111")
    store.add_membership("Program", "107638", "Activities", "222")

    assert sorted(store.list_members("Program", "107638", "Activities")) == ["111", "222"]

    store.remove_membership("Program", "107638", "Activities", "111")

    assert store.list_members("Program", "107638", "Activities") == ["222"]


def test_list_members_with_no_children_is_empty():
    store = EventStore(bucket_name="test-bucket", client=_FakeClient())

    assert store.list_members("Program", "no-such-program", "Activities") == []


def test_remove_membership_on_missing_marker_does_not_raise():
    store = EventStore(bucket_name="test-bucket", client=_FakeClient())

    store.remove_membership("Program", "107638", "Activities", "111")  # must not raise


def test_list_all_returns_only_documents_not_membership_markers():
    store = EventStore(bucket_name="test-bucket", client=_FakeClient())
    store.set("Activity", "111", {"name": "A"})
    store.set("Activity", "222", {"name": "B"})
    store.set("Program", "107638", {"name": "P"})
    store.add_membership("Program", "107638", "Activities", "111")

    assert sorted(store.list_all("Activity")) == ["111", "222"]
    assert store.list_all("Program") == ["107638"]


def test_list_all_on_empty_context_is_empty():
    store = EventStore(bucket_name="test-bucket", client=_FakeClient())

    assert store.list_all("Activity") == []
