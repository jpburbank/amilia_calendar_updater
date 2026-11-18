"""
Fixed values that shape what goes on the calendar, kept in one place so
they're easy to find and change. Edit here; nothing else needs to change.
"""

# The building every Activity takes place in. Amilia's location names are
# rooms inside it; the event location is "<room>, <address>".
BUILDING_ADDRESS = "39449 Queens Way #1, Squamish, BC V8B 0R5"

# Google Calendar's fixed event colours, by the name its UI shows, to the
# colorId the API takes (checked against GET /calendar/v3/colors on
# 2026-11-18). These 11 are the only colours an event can have.
GOOGLE_EVENT_COLOUR_IDS = {
    "Lavender": "1",
    "Sage": "2",
    "Grape": "3",
    "Flamingo": "4",
    "Banana": "5",
    "Tangerine": "6",
    "Peacock": "7",
    "Graphite": "8",
    "Blueberry": "9",
    "Basil": "10",
    "Tomato": "11",
}

# Event colour for each Amilia location (room) name, from the list above.
# Names match ignoring case and surrounding/repeated spaces. A room not
# listed here (or no room) gets no colour: the calendar's default.
LOCATION_COLOURS = {
    "Pottery Studio": "Basil",
    "Wood shop": "Banana",
    "Digital Fab Lab": "Blueberry",
    "CNC router": "Peacock",
    "3D Printer": "Blueberry",
    "Metal shop": "Tomato",
    "Laser": "Blueberry",
    "Textiles studio": "Lavender",
    "Event Space": "Graphite",
    "Create Makerspace": "Graphite",
    "Front Desk": "Graphite",
}
