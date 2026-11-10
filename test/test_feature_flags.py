import pytest

from shared.feature_flags import facility_booking_sync_enabled


def test_facility_booking_sync_is_off_when_unset():
    assert facility_booking_sync_enabled({}) is False


def test_facility_booking_sync_is_off_when_blank():
    assert facility_booking_sync_enabled({"FACILITY_BOOKING_SYNC_ENABLED": ""}) is False


@pytest.mark.parametrize("value", ["true", "TRUE", " True "])
def test_facility_booking_sync_is_on_when_true(value):
    assert facility_booking_sync_enabled({"FACILITY_BOOKING_SYNC_ENABLED": value}) is True


@pytest.mark.parametrize("value", ["false", "FALSE"])
def test_facility_booking_sync_is_off_when_false(value):
    assert facility_booking_sync_enabled({"FACILITY_BOOKING_SYNC_ENABLED": value}) is False


@pytest.mark.parametrize("value", ["ture", "1", "yes"])
def test_facility_booking_sync_rejects_unrecognized_values(value):
    with pytest.raises(ValueError, match="FACILITY_BOOKING_SYNC_ENABLED"):
        facility_booking_sync_enabled({"FACILITY_BOOKING_SYNC_ENABLED": value})
