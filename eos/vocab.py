"""RE photography vocabulary — single source of truth for rooms, property types, and channels."""

STUDIO_ID = "default"

LISTING_STATUSES = ("lead", "booked", "shooting", "editing", "delivered", "archived")
LISTING_STATUS_LABELS = {
    "lead": "Lead",
    "booked": "Booked",
    "shooting": "Shooting",
    "editing": "Editing",
    "delivered": "Delivered",
    "archived": "Archived",
}

PROPERTY_TYPES = ("residential", "commercial", "land", "multi_family", "other")
PROPERTY_TYPE_LABELS = {
    "residential": "Residential",
    "commercial": "Commercial",
    "land": "Land",
    "multi_family": "Multi-family",
    "other": "Other",
}

CLIENT_TYPES = ("agent", "brokerage", "homeowner", "vendor")
CLIENT_TYPE_LABELS = {
    "agent": "Agent",
    "brokerage": "Brokerage",
    "homeowner": "Homeowner",
    "vendor": "Vendor",
}

# Default gallery sections seeded on create (room-based, not F&B).
DEFAULT_SECTIONS = [
    "Exterior & Curb Appeal",
    "Living Areas",
    "Kitchen",
    "Bedrooms",
    "Bathrooms",
    "Details & Amenities",
]

# Shot-list room categories.
SHOT_ROOMS = [
    "exterior",
    "living",
    "kitchen",
    "bedroom",
    "bathroom",
    "detail",
    "aerial",
    "other",
]
SHOT_ROOM_LABELS = {
    "exterior": "Exterior",
    "living": "Living",
    "kitchen": "Kitchen",
    "bedroom": "Bedroom",
    "bathroom": "Bathroom",
    "detail": "Detail",
    "aerial": "Aerial",
    "other": "Other",
}

SHOT_PRIORITIES = ("must", "want", "if_time")
SHOT_PRIORITY_LABELS = {
    "must": "Must have",
    "want": "Want",
    "if_time": "If time",
}

# Default shot list template for a new listing.
DEFAULT_SHOT_LIST = [
    ("exterior", "Front elevation — straight-on", "must"),
    ("exterior", "Address / curb appeal angle", "must"),
    ("living", "Living room wide — natural light", "must"),
    ("kitchen", "Kitchen wide — countertops visible", "must"),
    ("bedroom", "Primary bedroom wide", "want"),
    ("bathroom", "Primary bath — vanity + shower/tub", "want"),
    ("detail", "Key amenity or unique feature", "if_time"),
]

DEFAULT_LISTING_TASKS = [
    "Confirm lockbox / access",
    "Shoot property",
    "Cull & edit",
    "Export MLS sizes",
    "Deliver gallery link",
    "Send invoice",
]

EXPORT_CHANNELS = ("mls", "zillow", "realtor", "instagram", "print")