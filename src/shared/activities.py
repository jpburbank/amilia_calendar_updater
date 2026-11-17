"""
What an Activity's stored record and calendar events contain. Shared by the
webhook handler, the reconciler, and scripts/backfill.py so all three write
the same thing.

`activity` is an Amilia Activity dict. The REST Activity and the Activity
webhook payload use the same field names; callers pass the REST copy
whenever they have it, because the webhook's StartDate/EndDate carry the
wrong UTC offset and it has no Schedules (the location fallback).

The record keeps everything an event needs (name, description, and each
occurrence's start/end/location), so the reconciler can create an event
without calling Amilia.
"""

from __future__ import annotations

import html
from urllib.parse import urlsplit

from shared.locations import event_location, occurrence_location, schedule_location
from shared.titles import clean_title

# Fields after Description, in description order, each with its paragraph header.
_HEADED_DESCRIPTION_FIELDS = (
    ("Note", "Note"),
    ("Prerequisite", "Prerequisite"),
    ("AdditionalInformation", "Additional Information"),
    ("ResponsibleName", "Contact person at organization"),
)


def _valid_url(value) -> str | None:
    """`value` stripped, if it's an absolute http(s) URL; otherwise None."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return url


def booking_url(activity: dict) -> str | None:
    """
    The activity's store page: Url if it's a valid http(s) URL, else
    SecretUrl if that is; None when neither is.
    """
    return _valid_url(activity.get("Url")) or _valid_url(activity.get("SecretUrl"))


def build_activity_description(activity: dict) -> str | None:
    """
    Builds the calendar event description: a "Book Now" link (see
    booking_url), then Description, then Note, Prerequisite,
    AdditionalInformation and ResponsibleName, each its own paragraph.
    Description has no header (Amilia already sends it as its own HTML
    paragraph(s)); the others each get an HTML header matching that same
    style. Anything null or empty — including the link, when there's no
    valid URL — is omitted entirely rather than shown blank.
    """
    paragraphs = []
    url = booking_url(activity)
    if url:
        paragraphs.append(f'<p><a href="{html.escape(url)}">Book Now</a></p>')
    description = activity.get("Description")
    if description:
        paragraphs.append(description)
    for field, header in _HEADED_DESCRIPTION_FIELDS:
        value = activity.get(field)
        if value:
            paragraphs.append(f"<p><strong>{header}</strong><br>{value}</p>")
    return "\n".join(paragraphs) if paragraphs else None


def activity_record(activity: dict, status: str | None) -> dict:
    """The stored Activity document, without its occurrences (callers add them)."""
    return {
        "name": clean_title(activity.get("Name")),
        "program_id": activity.get("ProgramId") or None,  # Amilia uses 0 for "no program"
        "category_id": activity.get("CategoryId") or None,
        "category_name": activity.get("CategoryName"),
        "sub_category_id": activity.get("SubCategoryId") or None,
        "sub_category_name": activity.get("SubCategoryName"),
        "status": status,
        "start_date": activity.get("StartDate"),
        "end_date": activity.get("EndDate"),
        "description": build_activity_description(activity),
        "location": schedule_location(activity),
    }


def stored_occurrence(occurrence: dict, activity: dict) -> dict:
    """One REST occurrence as stored on the record, before its calendar_event_id."""
    return {
        "start": occurrence["Start"],
        "end": occurrence["End"],
        "location": occurrence_location(occurrence, activity),
    }


def activity_event_fields(activity_id, record: dict, occurrence: dict) -> dict:
    """
    Keyword arguments for CalendarClient.create_event/update_event, from a
    stored record and one of its stored occurrences. The location is the
    occurrence's room (records written before per-occurrence locations fall
    back to the record's own) plus the building address.
    """
    return {
        "summary": clean_title(record.get("name"), "Activity"),
        "start_iso": occurrence["start"],
        "end_iso": occurrence["end"],
        "location": event_location(occurrence.get("location") or record.get("location")),
        "description": record.get("description"),
        "extended_properties": {"amilia_type": "Activity", "amilia_type_id": str(activity_id)},
    }
