"""
Environment-driven feature flags, shared by the webhook function and
scripts/backfill.py so both read each flag the same way.

FACILITY_BOOKING_SYNC_ENABLED: whether Amilia FacilityBookings (admin,
member, private-lesson, etc. reservations) are synced to the calendar.
Off by default: as of 2026-11 the calendar only needs Programs, Activities,
and Activity occurrences. The FacilityBooking code is kept intact rather
than deleted, so turning it back on is just setting this to "true" in
env.yaml and redeploying.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

FACILITY_BOOKING_SYNC_ENABLED = "FACILITY_BOOKING_SYNC_ENABLED"


def facility_booking_sync_enabled(environ: Mapping[str, str] = os.environ) -> bool:
    return _flag(environ, FACILITY_BOOKING_SYNC_ENABLED, default=False)


def _flag(environ: Mapping[str, str], name: str, *, default: bool) -> bool:
    """
    Parses "true"/"false" (any case). Anything else raises, so a typo like
    "ture" fails at startup rather than silently leaving the flag off.
    """
    value = environ.get(name)
    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f'{name} must be "true" or "false", got {value!r}')
