from pathlib import Path

from fastapi.templating import Jinja2Templates

from . import config
from .vocab import (
    CLIENT_TYPE_LABELS,
    LISTING_STATUS_LABELS,
    PROPERTY_TYPE_LABELS,
    SHOT_PRIORITY_LABELS,
    SHOT_ROOM_LABELS,
)

ROOT = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=ROOT / "templates")

templates.env.globals["site_name"] = config.SITE_NAME
templates.env.globals["base_url"] = config.BASE_URL
templates.env.globals["listing_status_labels"] = LISTING_STATUS_LABELS
templates.env.globals["property_type_labels"] = PROPERTY_TYPE_LABELS
templates.env.globals["client_type_labels"] = CLIENT_TYPE_LABELS
templates.env.globals["shot_room_labels"] = SHOT_ROOM_LABELS
templates.env.globals["shot_priority_labels"] = SHOT_PRIORITY_LABELS
templates.env.globals["static_rev"] = int(max(
    (f.stat().st_mtime for f in (ROOT / "static").glob("*") if f.is_file()),
    default=0,
))