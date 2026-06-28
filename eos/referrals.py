"""Agent referral credits — tracked codes applied at booking."""

from . import db
from .vocab import STUDIO_ID


def list_codes():
    return db.all_(
        """SELECT r.*, c.name AS referrer_name
           FROM referral_codes r
           LEFT JOIN clients c ON c.id=r.referrer_client_id
           WHERE r.studio_id=?
           ORDER BY r.code""",
        (str(STUDIO_ID),),
    )


def create_code(*, code: str, credit_cents: int, referrer_client_id: int | None = None, max_uses: int | None = None) -> int:
    code = code.strip().upper()
    rid = db.run(
        """INSERT INTO referral_codes
           (studio_id, code, credit_cents, referrer_client_id, max_uses)
           VALUES (?,?,?,?,?)""",
        (str(STUDIO_ID), code, credit_cents, referrer_client_id, max_uses),
    )
    db.audit("admin", "referral.create", f"code={code}")
    return rid


def lookup(code: str):
    code = code.strip().upper()
    if not code:
        return None
    return db.one(
        """SELECT * FROM referral_codes
           WHERE studio_id=? AND upper(code)=? AND active=1""",
        (str(STUDIO_ID), code),
    )


def apply_credit(code: str, total_cents: int) -> tuple[int, dict | None]:
    row = lookup(code)
    if not row:
        return total_cents, None
    if row["max_uses"] is not None and row["uses"] >= row["max_uses"]:
        return total_cents, None
    new_total = max(0, total_cents - row["credit_cents"])
    return new_total, row


def record_use(referral_id: int) -> None:
    db.run(
        "UPDATE referral_codes SET uses=uses+1 WHERE id=? AND studio_id=?",
        (referral_id, str(STUDIO_ID)),
    )