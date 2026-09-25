"""Tests for the standalone bulk-delete script's core logic."""

import argparse
from datetime import datetime, timedelta, timezone

from bulk_delete import (
    build_arg_parser,
    cleanup_memberships,
    delete_one,
    extract_calendar_event_ids,
    resolve_targets,
    selection_label,
)


class _FakeCalendarClient:
    def __init__(self, fail_ids=None):
        self.delete_calls = []
        self._fail_ids = set(fail_ids or [])

    def delete_event(self, event_id):
        if event_id in self._fail_ids:
            raise RuntimeError(f"boom: {event_id}")
        self.delete_calls.append(event_id)


class _FakeEventStore:
    def __init__(self, documents=None, memberships=None):
        self.documents = dict(documents or {})
        self.memberships = set(memberships or [])
        self.delete_calls = []

    def get(self, context, external_id):
        doc = self.documents.get((context, external_id))
        return dict(doc) if doc is not None else None

    def delete(self, context, external_id):
        self.documents.pop((context, external_id), None)
        self.delete_calls.append((context, external_id))

    def list_all(self, context):
        return [eid for (ctx, eid) in self.documents if ctx == context]

    def list_members(self, parent_context, parent_id, child_context):
        return [
            child_id
            for (pc, pid, cc, child_id) in self.memberships
            if pc == parent_context and pid == parent_id and cc == child_context
        ]

    def remove_membership(self, parent_context, parent_id, child_context, child_id):
        self.memberships.discard((parent_context, parent_id, child_context, child_id))


def _args(**overrides):
    defaults = dict(ids=None, program_id=None, older_than_days=None, all=False)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_selection_label_variants():
    assert selection_label(_args(ids=["A", "B"])) == "ids"
    assert selection_label(_args(program_id="107638")) == "program-107638"
    assert selection_label(_args(older_than_days=30)) == "olderthan30d"
    assert selection_label(_args(all=True)) == "all"


def test_resolve_targets_by_explicit_ids_dedupes_and_preserves_order():
    store = _FakeEventStore()

    result = resolve_targets(store, "Activity", _args(ids=["222", "111", "222"]))

    assert result == ["222", "111"]


def test_resolve_targets_by_program_id_uses_membership():
    store = _FakeEventStore(memberships={("Program", "107638", "Activities", "111")})

    result = resolve_targets(store, "Activity", _args(program_id="107638"))

    assert result == ["111"]


def test_resolve_targets_by_older_than_days():
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    store = _FakeEventStore(
        documents={
            ("Activity", "old"): {"updated_at": old},
            ("Activity", "recent"): {"updated_at": recent},
        }
    )

    result = resolve_targets(store, "Activity", _args(older_than_days=30))

    assert result == ["old"]


def test_resolve_targets_all_lists_every_document_in_context():
    store = _FakeEventStore(
        documents={("Activity", "111"): {}, ("Activity", "222"): {}, ("Program", "9"): {}}
    )

    result = sorted(resolve_targets(store, "Activity", _args(all=True)))

    assert result == ["111", "222"]


def test_extract_calendar_event_ids_activity_collects_all_occurrences():
    doc = {
        "occurrences": {
            "a": {"calendar_event_id": "evt_1"},
            "b": {"calendar_event_id": None},
            "c": {"calendar_event_id": "evt_2"},
        }
    }

    assert sorted(extract_calendar_event_ids("Activity", doc)) == ["evt_1", "evt_2"]


def test_extract_calendar_event_ids_facility_booking_single_field():
    assert extract_calendar_event_ids("FacilityBooking", {"calendar_event_id": "evt_9"}) == ["evt_9"]
    assert extract_calendar_event_ids("FacilityBooking", {"calendar_event_id": None}) == []


def test_extract_calendar_event_ids_program_has_none():
    assert extract_calendar_event_ids("Program", {"name": "x"}) == []


def test_cleanup_memberships_removes_activitys_own_marker():
    store = _FakeEventStore(memberships={("Program", "107638", "Activities", "111")})

    cleanup_memberships(store, "Activity", "111", {"program_id": "107638"})

    assert store.memberships == set()


def test_cleanup_memberships_removes_all_of_a_deleted_programs_markers():
    store = _FakeEventStore(
        memberships={
            ("Program", "107638", "Activities", "111"),
            ("Program", "107638", "Activities", "222"),
        }
    )

    cleanup_memberships(store, "Program", "107638", {})

    assert store.memberships == set()


def test_delete_one_target_both_deletes_calendar_events_and_store_doc():
    calendar_client = _FakeCalendarClient()
    store = _FakeEventStore(
        documents={
            ("Activity", "111"): {
                "program_id": "107638",
                "occurrences": {"a": {"calendar_event_id": "evt_1"}},
            }
        },
        memberships={("Program", "107638", "Activities", "111")},
    )

    result = delete_one(calendar_client, store, "Activity", "111", target="both")

    assert result["calendar_deleted"] == ["evt_1"]
    assert result["store_deleted"] is True
    assert calendar_client.delete_calls == ["evt_1"]
    assert ("Activity", "111") in store.delete_calls
    assert store.memberships == set()


def test_delete_one_target_store_only_leaves_calendar_untouched():
    calendar_client = _FakeCalendarClient()
    store = _FakeEventStore(
        documents={("Activity", "111"): {"occurrences": {"a": {"calendar_event_id": "evt_1"}}}}
    )

    result = delete_one(calendar_client, store, "Activity", "111", target="store")

    assert result["calendar_deleted"] == []
    assert calendar_client.delete_calls == []
    assert result["store_deleted"] is True


def test_delete_one_target_calendar_only_leaves_store_doc():
    calendar_client = _FakeCalendarClient()
    store = _FakeEventStore(
        documents={("Activity", "111"): {"occurrences": {"a": {"calendar_event_id": "evt_1"}}}}
    )

    result = delete_one(calendar_client, store, "Activity", "111", target="calendar")

    assert calendar_client.delete_calls == ["evt_1"]
    assert result["store_deleted"] is False
    assert store.documents[("Activity", "111")] is not None


def test_delete_one_records_calendar_failures_without_raising():
    calendar_client = _FakeCalendarClient(fail_ids={"evt_1"})
    store = _FakeEventStore(
        documents={("Activity", "111"): {"occurrences": {"a": {"calendar_event_id": "evt_1"}}}}
    )

    result = delete_one(calendar_client, store, "Activity", "111", target="both")

    assert result["calendar_failed"] == [("evt_1", "boom: evt_1")]
    assert result["store_deleted"] is True  # store deletion still proceeds


def test_delete_one_on_missing_document_is_a_noop_not_an_error():
    calendar_client = _FakeCalendarClient()
    store = _FakeEventStore()

    result = delete_one(calendar_client, store, "Activity", "ghost", target="both")

    assert result["calendar_deleted"] == []
    assert result["store_deleted"] is True


def test_arg_parser_requires_exactly_one_selection_mode():
    parser = build_arg_parser()

    args = parser.parse_args(
        ["--context", "Activity", "--program-id", "107638", "--execute", "--confirm-bucket", "b"]
    )

    assert args.program_id == "107638"
    assert args.target == "both"
    assert args.limit == 25


def test_arg_parser_rejects_two_selection_modes():
    parser = build_arg_parser()

    try:
        parser.parse_args(["--context", "Activity", "--program-id", "107638", "--all"])
        assert False, "expected SystemExit from argparse mutually exclusive group"
    except SystemExit:
        pass
