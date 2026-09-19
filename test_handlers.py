"""Tests for per-context webhook handlers."""

from handlers import handle_activity, handle_facility_booking, handle_program


class _FakeCalendarClient:
    def __init__(self):
        self.calls = []
        self.update_calls = []
        self.delete_calls = []
        self._next_id = 0

    def create_event(self, **kwargs):
        self.calls.append(kwargs)
        self._next_id += 1
        return {"id": f"evt_{self._next_id}"}

    def update_event(self, event_id, **fields):
        self.update_calls.append((event_id, fields))
        return {"id": event_id}

    def delete_event(self, event_id):
        self.delete_calls.append(event_id)


class _FakeEventStore:
    def __init__(self, documents=None, memberships=None):
        self.documents = dict(documents or {})  # {(context, external_id): fields_dict}
        self.memberships = set(memberships or set())
        self.set_calls = []

    def get(self, context, external_id):
        doc = self.documents.get((context, external_id))
        return dict(doc) if doc is not None else None

    def set(self, context, external_id, fields, custom_time=None):
        self.documents[(context, external_id)] = dict(fields)
        self.set_calls.append((context, external_id, dict(fields), custom_time))

    def delete(self, context, external_id):
        self.documents.pop((context, external_id), None)

    def add_membership(self, parent_context, parent_id, child_context, child_id):
        self.memberships.add((parent_context, parent_id, child_context, child_id))

    def remove_membership(self, parent_context, parent_id, child_context, child_id):
        self.memberships.discard((parent_context, parent_id, child_context, child_id))

    def list_members(self, parent_context, parent_id, child_context):
        return [
            child_id
            for (pc, pid, cc, child_id) in self.memberships
            if pc == parent_context and pid == parent_id and cc == child_context
        ]


class _FakeAmiliaClient:
    def __init__(self, occurrences=None):
        self.occurrences = occurrences if occurrences is not None else []

    def get_activity_occurrences(self, org_id, activity_id):
        return self.occurrences


class _FakeTaskQueue:
    def __init__(self):
        self.enqueued = []

    def enqueue_reconciliation(self, activity_id):
        self.enqueued.append(activity_id)


# ---------------------------------------------------------------------------
# FacilityBooking
# ---------------------------------------------------------------------------

def _booking_payload(**overrides):
    payload = {
        "ReservationId": "FB-1",
        "Start": "2026-01-01T10:00:00",
        "End": "2026-01-01T11:00:00",
    }
    payload.update(overrides)
    return payload


def test_facility_booking_create_appends_booking_to_title():
    calendar_client = _FakeCalendarClient()

    handle_facility_booking(
        action="Create",
        payload=_booking_payload(Title="Laser"),
        calendar_client=calendar_client,
        event_store=_FakeEventStore(),
    )

    assert calendar_client.calls[0]["summary"] == "Laser booking"


def test_facility_booking_create_appends_booking_to_default_title():
    calendar_client = _FakeCalendarClient()

    handle_facility_booking(
        action="Create",
        payload=_booking_payload(),
        calendar_client=calendar_client,
        event_store=_FakeEventStore(),
    )

    assert calendar_client.calls[0]["summary"] == "Facility Booking booking"


def test_facility_booking_create_stores_mapping():
    event_store = _FakeEventStore()

    handle_facility_booking(
        action="Create",
        payload=_booking_payload(),
        calendar_client=_FakeCalendarClient(),
        event_store=event_store,
    )

    doc = event_store.get("FacilityBooking", "FB-1")
    assert doc["calendar_event_id"] == "evt_1"
    assert event_store.set_calls[0][3] == "2026-01-01T11:00:00"  # custom_time


def test_facility_booking_update_uses_stored_mapping():
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore(
        documents={("FacilityBooking", "FB-1"): {"calendar_event_id": "evt_123"}}
    )

    result = handle_facility_booking(
        action="Update",
        payload=_booking_payload(Title="Laser"),
        calendar_client=calendar_client,
        event_store=event_store,
    )

    assert result == {
        "reservation_id": "FB-1",
        "calendar_event_id": "evt_123",
        "updated": True,
    }
    event_id, fields = calendar_client.update_calls[0]
    assert event_id == "evt_123"
    assert fields["summary"] == "Laser"


def test_facility_booking_update_with_no_mapping_skips():
    calendar_client = _FakeCalendarClient()

    result = handle_facility_booking(
        action="Update",
        payload=_booking_payload(),
        calendar_client=calendar_client,
        event_store=_FakeEventStore(),
    )

    assert result == {"reservation_id": "FB-1", "skipped": "no stored event mapping"}
    assert calendar_client.update_calls == []


def test_facility_booking_delete_removes_mapping_and_calendar_event():
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore(
        documents={("FacilityBooking", "FB-1"): {"calendar_event_id": "evt_123"}}
    )

    result = handle_facility_booking(
        action="Delete",
        payload={"Id": "FB-1"},
        calendar_client=calendar_client,
        event_store=event_store,
    )

    assert result == {"reservation_id": "FB-1", "deleted": True}
    assert calendar_client.delete_calls == ["evt_123"]
    assert event_store.get("FacilityBooking", "FB-1") is None


def test_facility_booking_delete_with_no_mapping_skips():
    calendar_client = _FakeCalendarClient()

    result = handle_facility_booking(
        action="Delete",
        payload={"Id": "FB-1"},
        calendar_client=calendar_client,
        event_store=_FakeEventStore(),
    )

    assert result == {"reservation_id": "FB-1", "skipped": "no stored event mapping"}
    assert calendar_client.delete_calls == []


# ---------------------------------------------------------------------------
# Program
# ---------------------------------------------------------------------------

def _program_payload(**overrides):
    payload = {
        "Id": 107638,
        "Name": "Classes and Workshops",
        "Url": "https://app.amilia.com/store/en/create-makerspace/shop/programs/107638",
        "Online": True,
        "StartDate": "2024-09-01T07:00:00-07:00",
        "ExpirationDate": "2030-01-01T08:00:00-08:00",
    }
    payload.update(overrides)
    return payload


def test_program_create_stores_document():
    event_store = _FakeEventStore()

    result = handle_program(
        action="Create",
        payload=_program_payload(),
        calendar_client=_FakeCalendarClient(),
        event_store=event_store,
        task_queue=_FakeTaskQueue(),
    )

    doc = event_store.get("Program", 107638)
    assert doc["online"] is True
    assert doc["name"] == "Classes and Workshops"
    assert result == {"program_id": 107638, "online": True}


def test_program_create_reconciles_activities_that_arrived_first():
    """An Activity's Create can beat its Program's Create — catch it on Program Create."""
    event_store = _FakeEventStore(memberships={("Program", 107638, "Activities", "7311901")})
    task_queue = _FakeTaskQueue()

    handle_program(
        action="Create",
        payload=_program_payload(),
        calendar_client=_FakeCalendarClient(),
        event_store=event_store,
        task_queue=task_queue,
    )

    assert task_queue.enqueued == ["7311901"]


def test_program_update_with_no_online_change_does_not_reconcile():
    event_store = _FakeEventStore(
        documents={("Program", 107638): {"online": True}},
        memberships={("Program", 107638, "Activities", "7311901")},
    )
    task_queue = _FakeTaskQueue()

    handle_program(
        action="Update",
        payload=_program_payload(Online=True),
        calendar_client=_FakeCalendarClient(),
        event_store=event_store,
        task_queue=task_queue,
    )

    assert task_queue.enqueued == []


def test_program_update_online_flip_reconciles_every_child():
    event_store = _FakeEventStore(
        documents={("Program", 107638): {"online": False}},
        memberships={
            ("Program", 107638, "Activities", "111"),
            ("Program", 107638, "Activities", "222"),
        },
    )
    task_queue = _FakeTaskQueue()

    handle_program(
        action="Update",
        payload=_program_payload(Online=True),
        calendar_client=_FakeCalendarClient(),
        event_store=event_store,
        task_queue=task_queue,
    )

    assert sorted(task_queue.enqueued) == ["111", "222"]


def test_program_delete_cascades_to_children():
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore(
        documents={
            ("Program", 107638): {"online": True},
            ("Activity", "111"): {"occurrences": {"a": {"calendar_event_id": "evt_a"}}},
            ("Activity", "222"): {"occurrences": {"b": {"calendar_event_id": "evt_b"}}},
        },
        memberships={
            ("Program", 107638, "Activities", "111"),
            ("Program", 107638, "Activities", "222"),
        },
    )

    result = handle_program(
        action="Delete",
        payload={"Id": 107638},
        calendar_client=calendar_client,
        event_store=event_store,
        task_queue=_FakeTaskQueue(),
    )

    assert sorted(calendar_client.delete_calls) == ["evt_a", "evt_b"]
    assert event_store.get("Activity", "111") is None
    assert event_store.get("Activity", "222") is None
    assert event_store.get("Program", 107638) is None
    assert event_store.list_members("Program", 107638, "Activities") == []
    assert result["activities_removed"] == 2


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------

def _activity_payload(**overrides):
    payload = {
        "Id": 7311901,
        "Name": "Test Activity",
        "ProgramId": 107638,
        "ProgramName": "Classes and Workshops",
        "CategoryId": 5525637,
        "CategoryName": "Metal Shop",
        "SubCategoryId": 5525638,
        "SubCategoryName": "Skill classes",
        "Status": "Normal",
        "StartDate": "2026-09-25T12:46:00-04:00",
        "EndDate": "2026-10-27T13:29:00-04:00",
        "LocationLabel": "Metal shop",
    }
    payload.update(overrides)
    return payload


_ONE_OCCURRENCE = [
    {"Id": 141658523, "Start": "2026-09-25T12:46:00-07:00", "End": "2026-09-25T13:46:00-07:00"}
]


def test_activity_create_with_unknown_program_is_visible_by_default():
    """Fail open: a Program we haven't seen yet is assumed Online."""
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore()

    result = handle_activity(
        action="Create",
        payload=_activity_payload(),
        calendar_client=calendar_client,
        event_store=event_store,
        org_id=17659,
        amilia_client=_FakeAmiliaClient(occurrences=_ONE_OCCURRENCE),
    )

    assert result["visible"] is True
    assert len(calendar_client.calls) == 1
    doc = event_store.get("Activity", 7311901)
    assert doc["occurrences"]["141658523"]["calendar_event_id"] == "evt_1"
    assert ("Program", 107638, "Activities", 7311901) in event_store.memberships


def test_activity_create_with_offline_program_is_not_visible():
    event_store = _FakeEventStore(documents={("Program", 107638): {"online": False}})
    calendar_client = _FakeCalendarClient()

    result = handle_activity(
        action="Create",
        payload=_activity_payload(),
        calendar_client=calendar_client,
        event_store=event_store,
        org_id=17659,
        amilia_client=_FakeAmiliaClient(occurrences=_ONE_OCCURRENCE),
    )

    assert result["visible"] is False
    assert calendar_client.calls == []
    doc = event_store.get("Activity", 7311901)
    # Occurrence data is still stored, just with no calendar event yet, so a
    # later Program-triggered reconciliation can create it without REST.
    assert doc["occurrences"]["141658523"]["calendar_event_id"] is None
    assert doc["occurrences"]["141658523"]["start"] == "2026-09-25T12:46:00-07:00"


def test_activity_create_hidden_status_is_not_visible_even_if_program_online():
    event_store = _FakeEventStore(documents={("Program", 107638): {"online": True}})
    calendar_client = _FakeCalendarClient()

    result = handle_activity(
        action="Create",
        payload=_activity_payload(Status="Hidden"),
        calendar_client=calendar_client,
        event_store=event_store,
        org_id=17659,
        amilia_client=_FakeAmiliaClient(occurrences=_ONE_OCCURRENCE),
    )

    assert result["visible"] is False
    assert calendar_client.calls == []


def test_activity_update_creates_new_occurrence_and_removes_dropped_one():
    event_store = _FakeEventStore(
        documents={
            ("Program", 107638): {"online": True},
            ("Activity", 7311901): {
                "program_id": 107638,
                "occurrences": {"111": {"start": "s", "end": "e", "calendar_event_id": "evt_old"}},
            },
        }
    )
    calendar_client = _FakeCalendarClient()
    new_occurrence = [
        {"Id": 222, "Start": "2026-11-01T10:00:00-07:00", "End": "2026-11-01T11:00:00-07:00"}
    ]

    handle_activity(
        action="Update",
        payload=_activity_payload(),
        calendar_client=calendar_client,
        event_store=event_store,
        org_id=17659,
        amilia_client=_FakeAmiliaClient(occurrences=new_occurrence),
    )

    assert calendar_client.delete_calls == ["evt_old"]
    doc = event_store.get("Activity", 7311901)
    assert "111" not in doc["occurrences"]
    assert doc["occurrences"]["222"]["calendar_event_id"] == "evt_1"


def test_activity_update_moves_membership_when_program_changes():
    event_store = _FakeEventStore(
        documents={("Activity", 7311901): {"program_id": 999, "occurrences": {}}},
        memberships={("Program", 999, "Activities", 7311901)},
    )

    handle_activity(
        action="Update",
        payload=_activity_payload(ProgramId=107638),
        calendar_client=_FakeCalendarClient(),
        event_store=event_store,
        org_id=17659,
        amilia_client=_FakeAmiliaClient(occurrences=[]),
    )

    assert ("Program", 999, "Activities", 7311901) not in event_store.memberships
    assert ("Program", 107638, "Activities", 7311901) in event_store.memberships


def test_activity_delete_removes_events_document_and_membership():
    calendar_client = _FakeCalendarClient()
    event_store = _FakeEventStore(
        documents={
            ("Activity", 7311901): {
                "program_id": 107638,
                "occurrences": {"111": {"calendar_event_id": "evt_a"}},
            }
        },
        memberships={("Program", 107638, "Activities", 7311901)},
    )

    result = handle_activity(
        action="Delete",
        payload={"Id": 7311901},
        calendar_client=calendar_client,
        event_store=event_store,
        org_id=17659,
        amilia_client=_FakeAmiliaClient(),
    )

    assert result == {"activity_id": 7311901, "deleted": True}
    assert calendar_client.delete_calls == ["evt_a"]
    assert event_store.get("Activity", 7311901) is None
    assert event_store.list_members("Program", 107638, "Activities") == []


def test_activity_delete_with_no_stored_activity_skips():
    result = handle_activity(
        action="Delete",
        payload={"Id": 7311901},
        calendar_client=_FakeCalendarClient(),
        event_store=_FakeEventStore(),
        org_id=17659,
        amilia_client=_FakeAmiliaClient(),
    )

    assert result == {"activity_id": 7311901, "skipped": "no stored activity"}
