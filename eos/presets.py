"""MLS / portal export presets — single source of truth."""

from . import db
from .vocab import STUDIO_ID


def active() -> list:
    return db.all_(
        "SELECT * FROM crop_presets WHERE studio_id=? AND active=1 ORDER BY sort, id",
        (STUDIO_ID,),
    )
