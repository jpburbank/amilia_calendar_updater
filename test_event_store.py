"""Tests for EventStore, against a fake storage.Client-shaped object."""

from datetime import datetime, timezone

import pytest
from google.api_core.exceptions import NotFound

from event_store import EventStore


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


class _FakeClient:
    def __init__(self):
        self._bucket = _FakeBucket()

    def bucket(self, name):
        return self._bucket


def test_set_then_get_round_trips_event_id():
    store = EventStore(bucket_name="test-bucket", client=_FakeClient())

    store.set("FacilityBooking", "FB-1", "evt_123", action="Create", end_iso="2026-01-01T11:00:00")

    assert store.get("FacilityBooking", "FB-1") == "evt_123"


def test_get_on_missing_key_returns_none():
    store = EventStore(bucket_name="test-bucket", client=_FakeClient())

    assert store.get("FacilityBooking", "FB-missing") is None


def test_delete_on_missing_key_does_not_raise():
    store = EventStore(bucket_name="test-bucket", client=_FakeClient())

    store.delete("FacilityBooking", "FB-missing")  # must not raise


def test_delete_removes_the_mapping():
    store = EventStore(bucket_name="test-bucket", client=_FakeClient())
    store.set("FacilityBooking", "FB-1", "evt_123", action="Create", end_iso="2026-01-01T11:00:00")

    store.delete("FacilityBooking", "FB-1")

    assert store.get("FacilityBooking", "FB-1") is None


def test_set_does_not_move_custom_time_earlier():
    store = EventStore(bucket_name="test-bucket", client=_FakeClient())
    store.set("FacilityBooking", "FB-1", "evt_123", action="Create", end_iso="2026-06-01T11:00:00")

    store.set("FacilityBooking", "FB-1", "evt_123", action="Update", end_iso="2026-01-01T09:00:00")

    blob = store._blob("FacilityBooking", "FB-1")
    blob.reload()
    assert blob.custom_time == datetime.fromisoformat("2026-06-01T11:00:00")
