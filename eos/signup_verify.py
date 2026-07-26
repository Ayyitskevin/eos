"""Signup email verification before studio activation."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastapi import HTTPException

from . import config, db, mailer, security, tenant

log = logging.getLogger("eos.signup_verify")
_COOLDOWN_MINUTES = 5
_CLAIM_STALE_MINUTES = 10


def needs_verification(studio_id: str) -> bool:
    if not config.SIGNUP_ENABLED:
        return False
    row = db.one("SELECT signup_verified FROM studio WHERE id=?", (studio_id,))
    return bool(row and not row["signup_verified"])


def _fingerprint(token: str) -> str:
    """Non-authenticating token identity that migration SQL can also derive."""
    return token[-12:]


def _audit(con: sqlite3.Connection, studio_id: str, action: str, detail: str) -> None:
    con.execute(
        "INSERT INTO audit_log (studio_id, actor, action, detail) VALUES (?, 'signup', ?, ?)",
        (studio_id, action, detail),
    )


def _claim_delivery(
    studio_id: str,
    *,
    email: str,
    resend: bool,
) -> dict[str, Any]:
    """Atomically bind the current token and claim one provider attempt."""
    outcome: dict[str, Any]
    with db.tx(immediate=True) as con:
        studio = con.execute(
            """SELECT contact_email, signup_verified, signup_verify_token
               FROM studio WHERE id=? AND active=1""",
            (studio_id,),
        ).fetchone()
        if not studio:
            return {"status": "missing"}
        recipient = (studio["contact_email"] or "").strip()
        if not recipient:
            return {"status": "missing-recipient"}
        if email.strip().casefold() != recipient.casefold():
            return {"status": "recipient-mismatch"}
        if studio["signup_verified"]:
            return {"status": "verified"}

        token = studio["signup_verify_token"] or security.new_token()
        fingerprint = _fingerprint(token)
        intent = con.execute(
            """SELECT token_fingerprint, status, claimed_at, sent_at, attempts
               FROM signup_verification_intents WHERE studio_id=?""",
            (studio_id,),
        ).fetchone()

        if intent and intent["token_fingerprint"] == fingerprint:
            if intent["status"] == "unknown":
                return {"status": "unknown", "token": token}
            if intent["status"] == "claimed":
                stale = con.execute(
                    """SELECT ? <= datetime('now', ?) AS stale""",
                    (intent["claimed_at"], f"-{_CLAIM_STALE_MINUTES} minutes"),
                ).fetchone()["stale"]
                if not stale:
                    return {"status": "in-progress", "token": token}
                con.execute(
                    """UPDATE signup_verification_intents
                       SET status='unknown',
                           error='Delivery claim expired without a confirmed provider outcome',
                           claim_token=NULL,
                           updated_at=datetime('now')
                       WHERE studio_id=? AND status='claimed'""",
                    (studio_id,),
                )
                _audit(
                    con,
                    studio_id,
                    "signup.verification.unknown",
                    "stale delivery claim requires provider review",
                )
                return {"status": "unknown", "token": token}
            if intent["status"] == "sent":
                if not intent["sent_at"]:
                    con.execute(
                        """UPDATE signup_verification_intents
                           SET status='unknown', claim_token=NULL,
                               error='Sent delivery is missing its confirmation timestamp',
                               updated_at=datetime('now')
                           WHERE studio_id=? AND status='sent'""",
                        (studio_id,),
                    )
                    _audit(
                        con,
                        studio_id,
                        "signup.verification.unknown",
                        "sent delivery is missing confirmation timestamp",
                    )
                    return {"status": "unknown", "token": token}
                if not resend:
                    return {"status": "already-sent", "token": token}
                cooling_down = con.execute(
                    """SELECT ? >= datetime('now', ?) AS cooling_down""",
                    (intent["sent_at"], f"-{_COOLDOWN_MINUTES} minutes"),
                ).fetchone()["cooling_down"]
                if cooling_down:
                    return {"status": "cooldown", "token": token}

        claim_token = security.new_token()
        con.execute(
            """UPDATE studio
               SET signup_verified=0, signup_verify_token=?,
                   signup_verify_issued_at=datetime('now')
               WHERE id=? AND signup_verified=0""",
            (token, studio_id),
        )
        con.execute(
            """INSERT INTO signup_verification_intents
               (studio_id, token_fingerprint, to_email, claim_token)
               VALUES (?,?,?,?)
               ON CONFLICT(studio_id) DO UPDATE SET
                   token_fingerprint=excluded.token_fingerprint,
                   to_email=excluded.to_email,
                   status='claimed',
                   attempts=signup_verification_intents.attempts+1,
                   generation=signup_verification_intents.generation+1,
                   claim_token=excluded.claim_token,
                   claimed_at=datetime('now'), sent_at=NULL, error=NULL,
                   updated_at=datetime('now')""",
            (studio_id, fingerprint, recipient, claim_token),
        )
        outcome = {
            "status": "claimed",
            "token": token,
            "to_email": recipient,
            "claim_token": claim_token,
        }
    return outcome


def _mark_delivery(
    studio_id: str,
    claim_token: str,
    status: str,
    error: str | None = None,
) -> None:
    action = f"signup.verification.{status}"
    with db.tx(immediate=True) as con:
        current = con.execute(
            "SELECT status FROM signup_verification_intents WHERE studio_id=?",
            (studio_id,),
        ).fetchone()
        if current and current["status"] == "verified":
            return
        cur = con.execute(
            """UPDATE signup_verification_intents
               SET status=?, sent_at=CASE WHEN ?='sent' THEN datetime('now') ELSE sent_at END,
                   claim_token=NULL, error=?, updated_at=datetime('now')
               WHERE studio_id=? AND status='claimed' AND claim_token=?""",
            (status, status, error[:300] if error else None, studio_id, claim_token),
        )
        if cur.rowcount != 1:
            raise RuntimeError("signup verification delivery claim is no longer active")
        _audit(con, studio_id, action, (error or "provider accepted")[:160])


def _mark_sent(studio_id: str, claim_token: str) -> None:
    _mark_delivery(studio_id, claim_token, "sent")


def _mark_failed(
    studio_id: str,
    claim_token: str,
    exc: Exception,
    *,
    outcome_unknown: bool,
) -> None:
    _mark_delivery(
        studio_id,
        claim_token,
        "unknown" if outcome_unknown else "failed",
        str(exc),
    )


def _auto_verify_without_mail(studio_id: str, *, email: str) -> str:
    with db.tx(immediate=True) as con:
        studio = con.execute(
            """SELECT contact_email, signup_verified, signup_verify_token
               FROM studio WHERE id=? AND active=1""",
            (studio_id,),
        ).fetchone()
        if not studio:
            raise HTTPException(status_code=404)
        recipient = (studio["contact_email"] or "").strip()
        if email.strip().casefold() != recipient.casefold():
            raise HTTPException(status_code=409, detail="Verification recipient changed.")
        token = studio["signup_verify_token"] or security.new_token()
        con.execute(
            """UPDATE studio SET signup_verified=1, signup_verify_token=NULL,
               signup_verify_issued_at=NULL WHERE id=?""",
            (studio_id,),
        )
        con.execute(
            """UPDATE signup_verification_intents
               SET status='verified', claim_token=NULL, error=NULL,
                   updated_at=datetime('now')
               WHERE studio_id=?""",
            (studio_id,),
        )
        return token


def issue_token(studio_id: str, *, email: str, resend: bool = False) -> str:
    """Deliver or idempotently recover the current studio verification token."""
    if not mailer.configured():
        if not config.SIGNUP_AUTO_VERIFY_LOCAL:
            raise RuntimeError(
                "Signup verification email is not configured; hosted signup remains unverified."
            )
        log.warning("mailer not configured — auto-verifying studio %s", studio_id)
        return _auto_verify_without_mail(studio_id, email=email)

    claim = _claim_delivery(studio_id, email=email, resend=resend)
    status = claim["status"]
    if status == "missing":
        raise HTTPException(status_code=404)
    if status == "missing-recipient":
        raise HTTPException(status_code=409, detail="Studio verification email is missing.")
    if status == "recipient-mismatch":
        raise HTTPException(status_code=409, detail="Verification recipient changed.")
    if status == "verified":
        raise HTTPException(status_code=409, detail="Studio is already verified.")
    if status == "cooldown":
        raise HTTPException(status_code=429, detail="Wait five minutes before resending.")
    if status == "in-progress":
        raise HTTPException(status_code=409, detail="Verification delivery is already in progress.")
    if status == "unknown":
        raise HTTPException(
            status_code=409,
            detail=(
                "Previous verification delivery outcome is unknown. "
                "Verify the provider before retrying."
            ),
        )
    if status == "already-sent":
        return str(claim["token"])

    token = str(claim["token"])
    claim_token = str(claim["claim_token"])
    url = _verify_url(studio_id, token)
    try:
        mailer.send_platform(
            str(claim["to_email"]),
            f"Verify your Eos studio — {studio_id}",
            f"Click to activate your studio:\n\n{url}\n\nThis link expires in 48 hours.",
        )
    except mailer.DeliveryOutcomeUnknown as exc:
        _mark_failed(studio_id, claim_token, exc, outcome_unknown=True)
        raise
    except Exception as exc:
        _mark_failed(studio_id, claim_token, exc, outcome_unknown=False)
        raise

    try:
        _mark_sent(studio_id, claim_token)
    except Exception as exc:
        try:
            _mark_failed(studio_id, claim_token, exc, outcome_unknown=True)
        except Exception:
            # The durable claim itself remains closed to replay if storage is unavailable.
            pass
        raise mailer.DeliveryOutcomeUnknown(
            "Verification email was accepted but local confirmation failed; "
            "verify the provider before retrying."
        ) from exc
    return token


def delivery_status(studio_id: str) -> dict[str, Any] | None:
    row = db.one(
        """SELECT status, attempts, claimed_at, sent_at, error,
                  CASE
                    WHEN status='unknown'
                      OR (status='claimed' AND claimed_at <= datetime('now', ?))
                    THEN 1 ELSE 0
                  END AS reconcilable
           FROM signup_verification_intents WHERE studio_id=?""",
        (f"-{_CLAIM_STALE_MINUTES} minutes", studio_id),
    )
    return dict(row) if row else None


@db.transactional(immediate=True)
def reconcile_delivery(studio_id: str, *, delivered: bool) -> str:
    """Record an operator-confirmed provider outcome without crossing tenants."""
    row = db.one(
        """SELECT status, claimed_at FROM signup_verification_intents
           WHERE studio_id=?""",
        (studio_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Verification delivery not found.")
    stale_claim = bool(
        row["status"] == "claimed"
        and db.one(
            "SELECT ? <= datetime('now', ?) AS stale",
            (row["claimed_at"], f"-{_CLAIM_STALE_MINUTES} minutes"),
        )["stale"]
    )
    if row["status"] == "claimed" and not stale_claim:
        raise HTTPException(status_code=409, detail="Verification delivery is still in progress.")
    if row["status"] != "unknown" and not stale_claim:
        raise HTTPException(
            status_code=409,
            detail="Verification delivery does not require reconciliation.",
        )

    status = "sent" if delivered else "failed"
    error = None if delivered else "Operator confirmed provider did not deliver"
    db.run(
        """UPDATE signup_verification_intents
           SET status=?, sent_at=CASE WHEN ?='sent' THEN COALESCE(sent_at, datetime('now'))
                                      ELSE NULL END,
               claim_token=NULL, error=?, updated_at=datetime('now')
           WHERE studio_id=?""",
        (status, status, error, studio_id),
    )
    if delivered:
        db.run(
            """UPDATE studio SET provisioning_status='ready', provisioning_error=NULL
               WHERE id=? AND provisioning_error LIKE 'verification:%'""",
            (studio_id,),
        )
    else:
        db.run(
            """UPDATE studio
               SET provisioning_status='degraded',
                   provisioning_error='verification: provider confirmed not delivered'
               WHERE id=? AND (
                   provisioning_error IS NULL OR provisioning_error LIKE 'verification:%'
               )""",
            (studio_id,),
        )
    db.run(
        """INSERT INTO audit_log (studio_id, actor, action, detail)
           VALUES (?, 'admin', ?, 'provider outcome confirmed')""",
        (studio_id, f"signup.verification.reconciled.{status}"),
    )
    return status


def _verify_url(studio_id: str, token: str) -> str:
    row = db.one("SELECT slug FROM studio WHERE id=?", (studio_id,))
    if config.BASE_DOMAIN and row and row["slug"]:
        base = tenant.studio_origin(slug=row["slug"])
    else:
        base = config.BASE_URL
    return f"{base}/verify/{token}"


@db.transactional(immediate=True)
def mark_verified(studio_id: str) -> None:
    db.run(
        """UPDATE studio SET signup_verified=1, signup_verify_token=NULL,
           signup_verify_issued_at=NULL WHERE id=?""",
        (studio_id,),
    )
    db.run(
        """UPDATE signup_verification_intents
           SET status='verified', claim_token=NULL, error=NULL,
               updated_at=datetime('now')
           WHERE studio_id=?""",
        (studio_id,),
    )


@db.transactional(immediate=True)
def verify_token(token: str) -> str:
    row = db.one(
        """SELECT id, slug FROM studio
           WHERE signup_verify_token=? AND active=1
             AND signup_verify_issued_at >= datetime('now','-48 hours')""",
        (token,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Invalid or expired verification link.")
    mark_verified(row["id"])
    tenant.set_studio(row["id"])
    db.audit("signup", "studio.verified", f"id={row['id']}")
    return row["slug"]


def resend(studio_id: str) -> str:
    """Retry a definite failure or resend a confirmed delivery after cooldown."""
    row = db.one(
        """SELECT contact_email, signup_verified
           FROM studio WHERE id=? AND active=1""",
        (studio_id,),
    )
    if not row:
        raise HTTPException(status_code=404)
    if row["signup_verified"]:
        raise HTTPException(status_code=409, detail="Studio is already verified.")
    try:
        token = issue_token(
            studio_id,
            email=row["contact_email"],
            resend=True,
        )
    except HTTPException:
        raise
    except mailer.DeliveryOutcomeUnknown as exc:
        raise HTTPException(
            status_code=502,
            detail="Verification email outcome is unknown; verify the provider before retrying.",
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Verification email failed.") from exc
    db.run(
        """UPDATE studio SET provisioning_status='ready', provisioning_error=NULL
           WHERE id=? AND provisioning_error LIKE 'verification:%'""",
        (studio_id,),
    )
    return token
