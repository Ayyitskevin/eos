"""Agent referral credits — tracked codes applied at booking."""

import datetime as dt
import secrets

from fastapi import HTTPException

from . import db
from .vocab import STUDIO_ID

AUTO_REFERRAL_MAX_USES = 25
_AUTO_CODE_ATTEMPTS = 8


def list_codes():
    return db.all_(
        """SELECT r.*, c.name AS referrer_name
           FROM referral_codes r
           LEFT JOIN clients c ON c.id=r.referrer_client_id AND c.studio_id=r.studio_id
           WHERE r.studio_id=?
           ORDER BY r.code""",
        (str(STUDIO_ID),),
    )


def create_code(
    *,
    code: str,
    credit_cents: int,
    referrer_client_id: int | None = None,
    max_uses: int | None = None,
) -> int:
    code = code.strip().upper()
    if referrer_client_id is not None:
        row = db.one(
            "SELECT id FROM clients WHERE id=? AND studio_id=?",
            (referrer_client_id, STUDIO_ID),
        )
        if not row:
            raise HTTPException(status_code=404)
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


def reserve_use(
    referral_id: int,
    *,
    inquiry_id: int,
    referred_client_id: int | None,
    expires_at: str,
):
    """Reserve a finite code use until the caller's final payment cutoff.

    Payment callers must include any accepted provider-webhook grace in ``expires_at``.
    """
    studio_id = str(STUDIO_ID)
    with db.tx(immediate=True) as con:
        expiry = con.execute(
            "SELECT datetime(?) AS value, datetime(?) > datetime('now') AS valid",
            (expires_at, expires_at),
        ).fetchone()
        if not expiry["value"] or not expiry["valid"]:
            raise HTTPException(
                status_code=400,
                detail="Referral reservation expiry must be future.",
            )
        inquiry = con.execute(
            "SELECT client_id FROM inquiries WHERE id=? AND studio_id=?",
            (inquiry_id, studio_id),
        ).fetchone()
        if not inquiry:
            raise HTTPException(status_code=404)
        inquiry_client_id = inquiry["client_id"]
        if inquiry_client_id is not None:
            if referred_client_id is not None and referred_client_id != inquiry_client_id:
                raise HTTPException(
                    status_code=409,
                    detail="Referral client does not match booking.",
                )
            referred_client_id = inquiry_client_id
        if referred_client_id is not None:
            client = con.execute(
                "SELECT id FROM clients WHERE id=? AND studio_id=?",
                (referred_client_id, studio_id),
            ).fetchone()
            if not client:
                raise HTTPException(status_code=404)
        existing = con.execute(
            """SELECT * FROM referral_redemptions
               WHERE studio_id=? AND inquiry_id=?""",
            (studio_id, inquiry_id),
        ).fetchone()
        if existing:
            if existing["referral_id"] != referral_id:
                raise HTTPException(
                    status_code=409,
                    detail="Booking already has a different referral code.",
                )
            if (
                referred_client_id is not None
                and existing["referred_client_id"] is not None
                and existing["referred_client_id"] != referred_client_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Referral client does not match booking.",
                )
            if existing["status"] == "confirmed":
                return existing
            if (
                existing["status"] == "reserved"
                and con.execute(
                    "SELECT datetime(?) > datetime('now') AS valid",
                    (existing["expires_at"],),
                ).fetchone()["valid"]
            ):
                return existing
            raise HTTPException(
                status_code=409,
                detail="Referral reservation is no longer active.",
            )
        code = con.execute(
            "SELECT * FROM referral_codes WHERE id=? AND studio_id=?",
            (referral_id, studio_id),
        ).fetchone()
        if not code or not code["active"]:
            raise HTTPException(
                status_code=409,
                detail="Referral code is no longer available.",
            )
        if referred_client_id is not None and code["referrer_client_id"] == referred_client_id:
            raise HTTPException(status_code=409, detail="Self-referrals are not allowed.")
        reserved = con.execute(
            """SELECT COUNT(*) AS n FROM referral_redemptions
               WHERE studio_id=? AND referral_id=? AND status='reserved'""",
            (studio_id, referral_id),
        ).fetchone()["n"]
        if code["max_uses"] is not None and code["uses"] + reserved >= code["max_uses"]:
            raise HTTPException(
                status_code=409,
                detail="Referral code is no longer available.",
            )
        con.execute(
            """INSERT INTO referral_redemptions
               (studio_id, referral_id, referrer_client_id, referral_code,
                inquiry_id, referred_client_id, credit_cents, status, expires_at)
               VALUES (?,?,?,?,?,?,?,'reserved',?)""",
            (
                studio_id,
                referral_id,
                code["referrer_client_id"],
                code["code"],
                inquiry_id,
                referred_client_id,
                code["credit_cents"],
                expiry["value"],
            ),
        )
        return con.execute(
            "SELECT * FROM referral_redemptions WHERE studio_id=? AND inquiry_id=?",
            (studio_id, inquiry_id),
        ).fetchone()


def finalize_inquiry(inquiry_id: int, *, payment_event_created: float | None = None) -> bool:
    """Confirm one reservation exactly once and consume its referral use."""
    studio_id = str(STUDIO_ID)
    with db.tx(immediate=True) as con:
        redemption = con.execute(
            """SELECT rr.*, q.payment_expires_at
               FROM referral_redemptions rr
               JOIN inquiries q ON q.id=rr.inquiry_id AND q.studio_id=rr.studio_id
               WHERE rr.studio_id=? AND rr.inquiry_id=?""",
            (studio_id, inquiry_id),
        ).fetchone()
        if not redemption:
            raise HTTPException(status_code=409, detail="Referral reservation does not exist.")
        if redemption["status"] == "confirmed":
            return False
        if redemption["status"] != "reserved":
            raise HTTPException(status_code=409, detail="Referral reservation is not active.")
        if payment_event_created is None:
            active = con.execute(
                "SELECT datetime(?) > datetime('now') AS valid",
                (redemption["expires_at"],),
            ).fetchone()["valid"]
        else:
            try:
                event_at = dt.datetime.fromtimestamp(float(payment_event_created), tz=dt.UTC)
                payment_expires_at = dt.datetime.strptime(
                    redemption["payment_expires_at"][:19], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=dt.UTC)
            except (TypeError, ValueError, OSError) as exc:
                raise HTTPException(
                    status_code=409, detail="Referral payment timing is invalid."
                ) from exc
            active = event_at <= payment_expires_at
        if not active:
            raise HTTPException(status_code=409, detail="Referral reservation has expired.")
        code = con.execute(
            """UPDATE referral_codes SET uses=uses+1
               WHERE id=? AND studio_id=?
                 AND (max_uses IS NULL OR uses < max_uses)""",
            (redemption["referral_id"], studio_id),
        )
        if code.rowcount != 1:
            raise HTTPException(status_code=409, detail="Referral code is no longer available.")
        finalized = con.execute(
            """UPDATE referral_redemptions
               SET status='confirmed', finalized_at=datetime('now')
               WHERE id=? AND studio_id=? AND status='reserved'""",
            (redemption["id"], studio_id),
        )
        if finalized.rowcount != 1:
            raise RuntimeError("referral reservation finalization lost its claim")
        return True


def release_inquiry(inquiry_id: int) -> bool:
    """Release an unpaid reservation without consuming a referral use."""
    with db.tx(immediate=True) as con:
        released = con.execute(
            """UPDATE referral_redemptions
               SET status='released', finalized_at=datetime('now')
               WHERE studio_id=? AND inquiry_id=? AND status='reserved'""",
            (str(STUDIO_ID), inquiry_id),
        )
        return released.rowcount == 1


def record_use(
    referral_id: int,
    *,
    inquiry_id: int | None = None,
    referred_client_id: int | None = None,
) -> None:
    """Immediately confirm a use; booking deposits should reserve then finalize instead."""
    studio_id = str(STUDIO_ID)
    with db.tx(immediate=True) as con:
        if inquiry_id is not None:
            expiry = con.execute("SELECT datetime('now','+1 hour') AS value").fetchone()["value"]
            reserve_use(
                referral_id,
                inquiry_id=inquiry_id,
                referred_client_id=referred_client_id,
                expires_at=expiry,
            )
            finalize_inquiry(inquiry_id)
            return
        cur = con.execute(
            """UPDATE referral_codes SET uses=uses+1
               WHERE id=? AND studio_id=? AND active=1
                 AND (max_uses IS NULL OR uses < max_uses)
                 AND (? IS NULL OR referrer_client_id IS NULL OR referrer_client_id != ?)""",
            (referral_id, studio_id, referred_client_id, referred_client_id),
        )
        if cur.rowcount != 1:
            row = con.execute(
                "SELECT referrer_client_id FROM referral_codes WHERE id=? AND studio_id=?",
                (referral_id, studio_id),
            ).fetchone()
            if (
                row
                and referred_client_id is not None
                and row["referrer_client_id"] == referred_client_id
            ):
                raise HTTPException(status_code=409, detail="Self-referrals are not allowed.")
            raise HTTPException(status_code=409, detail="Referral code is no longer available.")


def code_for_client(client_id: int):
    return db.one(
        """SELECT * FROM referral_codes
           WHERE studio_id=? AND referrer_client_id=? AND active=1
           ORDER BY id LIMIT 1""",
        (str(STUDIO_ID), client_id),
    )


def ensure_for_client(client_id: int):
    """Return one stable client code, creating a finite high-entropy code atomically."""
    studio_id = str(STUDIO_ID)
    with db.tx(immediate=True) as con:
        row = con.execute(
            """SELECT * FROM referral_codes
               WHERE studio_id=? AND referrer_client_id=? AND active=1
               ORDER BY id LIMIT 1""",
            (studio_id, client_id),
        ).fetchone()
        if row:
            return row
        client = con.execute(
            "SELECT id FROM clients WHERE id=? AND studio_id=?",
            (client_id, studio_id),
        ).fetchone()
        if not client:
            raise HTTPException(status_code=404)
        for _attempt in range(_AUTO_CODE_ATTEMPTS):
            code = f"REF-{secrets.token_hex(16).upper()}"
            cur = con.execute(
                """INSERT OR IGNORE INTO referral_codes
                   (studio_id, code, credit_cents, referrer_client_id, max_uses)
                   VALUES (?,?,2500,?,?)""",
                (studio_id, code, client_id, AUTO_REFERRAL_MAX_USES),
            )
            if cur.rowcount == 1:
                db.audit("system", "referral.ensure", f"client={client_id} code={code}")
                return con.execute(
                    "SELECT * FROM referral_codes WHERE id=? AND studio_id=?",
                    (cur.lastrowid, studio_id),
                ).fetchone()
    raise RuntimeError("could not allocate a unique referral code")
