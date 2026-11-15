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

from shared.locations import occurrence_location, schedule_location
from shared.titles import clean_title


def build_activity_description(activity: dict) -> str | None:
    """
    Builds the calendar event description from Description/Note/Prerequisite,
    each its own paragraph. Description has no header (Amilia already sends
    it as its own HTML paragraph(s)); Note and Prerequisite each get an HTML
    header matching that same style, and are omitted entirely when null or
    empty rather than shown blank.
    """
    paragraphs = []
    description = activity.get("Description")
    if description:
        paragraphs.append(description)
    note = activity.get("Note")
    if note:
        paragraphs.append(f"<p><strong>Note</strong><br>{note}</p>")
    prerequisite = activity.get("Prerequisite")
    if prerequisite:
        paragraphs.append(f"<p><strong>Prerequisite</strong><br>{prerequisite}</p>")
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
    stored record and one of its stored occurrences. Records written before
    per-occurrence locations fall back to the record's own location.
    """
    return {
        "summary": clean_title(record.get("name"), "Activity"),
        "start_iso": occurrence["start"],
        "end_iso": occurrence["end"],
        "location": occurrence.get("location") or record.get("location"),
        "description": record.get("description"),
        "extended_properties": {"amilia_type": "Activity", "amilia_type_id": str(activity_id)},
    }
