"""
Picks the calendar location for one Activity occurrence. Shared by the
webhook function, the reconciler, and scripts/backfill.py so all three agree.

Location belongs to an occurrence, not an Activity. The Activity webhook
never carries it, so it comes from the REST API, in this order:
1. the occurrence's own Location.Name (GET .../activities/{id}/occurrences);
2. failing that, the Activity's Schedules[].Locations[].Name
   (GET .../activities/{id}), every distinct name, comma-joined.

Those names are rooms inside one building, so the calendar event's location
(event_location) is the room followed by the building's street address —
the address is what lets Google Calendar link the event to Google Maps.
Records store only the room; the address is added when an event is built.
"""

from __future__ import annotations

from shared.titles import clean_title

# Where every Activity takes place.
BUILDING_ADDRESS = "39449 Queens Way #1, Squamish, BC V8B 0R5"


def occurrence_location(occurrence: dict, activity: dict | None = None) -> str | None:
    name = clean_title((occurrence.get("Location") or {}).get("Name"))
    if name:
        return name
    return schedule_location(activity)


def schedule_location(activity: dict | None) -> str | None:
    """Every distinct location name across the Activity's schedules, in order."""
    names = []
    for schedule in (activity or {}).get("Schedules") or []:
        for location in schedule.get("Locations") or []:
            name = clean_title(location.get("Name"))
            if name and name not in names:
                names.append(name)
    return ", ".join(names) or None


def event_location(room: str | None) -> str:
    """The calendar location: "<room>, <building address>", or just the address."""
    return f"{room}, {BUILDING_ADDRESS}" if room else BUILDING_ADDRESS
