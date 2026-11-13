"""
Normalizes Amilia titles (Program/Activity Name, FacilityBooking Title)
before they're stored or written to the calendar. Shared by the webhook
function, the reconciler, and scripts/backfill.py so all three agree.

Amilia keeps leading/trailing whitespace exactly as typed (live payloads
carry names like "Bandsaw Bear Claws  "), so every title is stripped.
Safe because a title is display-only: nothing looks a record up by name —
lookups always go through Amilia IDs.
"""

from __future__ import annotations


def clean_title(value: str | None, default: str | None = None) -> str | None:
    """Strips surrounding whitespace; a missing or blank title becomes `default`."""
    stripped = (value or "").strip()
    return stripped or default
