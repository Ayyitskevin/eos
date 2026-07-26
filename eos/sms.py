"""Twilio SMS — shoot-day reminders and sequence channel."""

import datetime as dt
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from . import config, db, security
from .vocab import STUDIO_ID

log = logging.getLogger("eos.sms")


def configured() -> bool:
    return bool(
        config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN and config.TWILIO_FROM_NUMBER
    )


class SmsOutcomeUnknown(RuntimeError):
    """Twilio accepted the message but local persistence is uncertain."""


def _log_sms(to_phone: str, body: str, status: str) -> None:
    db.run(
        """INSERT INTO sms_log (studio_id, to_phone, body, status)
           VALUES (?,?,?,?)""",
        (STUDIO_ID, to_phone, body.strip()[:500], status),
    )


def send(*, to_phone: str, body: str) -> bool:
    to_phone = "".join(c for c in to_phone if c.isdigit() or c == "+")
    if not to_phone or not body.strip():
        return False
    if not configured():
        log.info("sms stub to=%s body=%s", to_phone, body[:80])
        _log_sms(to_phone, body, "stub")
        return False
    url = f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_ACCOUNT_SID}/Messages.json"
    try:
        response = httpx.post(
            url,
            auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN),
            data={"From": config.TWILIO_FROM_NUMBER, "To": to_phone, "Body": body.strip()[:1600]},
            timeout=30,
        )
    except Exception as exc:
        raise SmsOutcomeUnknown(
            "SMS outcome unknown after Twilio dispatch; verify Twilio before retrying."
        ) from exc
    try:
        response.raise_for_status()
    except Exception as exc:
        log.error("sms provider request failed: %s", exc)
        try:
            _log_sms(to_phone, body, "failed")
        except Exception:
            log.exception("could not persist failed SMS attempt")
        return False
    try:
        _log_sms(to_phone, body, "sent")
    except Exception as exc:
        raise SmsOutcomeUnknown(
            "SMS outcome unknown after Twilio acceptance; verify Twilio before retrying."
        ) from exc
    return True


_CLAIM_TIMEOUT = "-15 minutes"
_UNKNOWN_OUTCOME = "SMS outcome unknown; verify Twilio before retrying."
_NOT_DELIVERED = "Operator confirmed the shoot-day SMS was not delivered."


class SmsReminderNotFound(LookupError):
    """The requested reminder does not belong to the active studio."""


class SmsReminderReconciliationConflict(RuntimeError):
    """The reminder cannot be safely reconciled in its current state."""


def _zone_from_name(name: str | None) -> ZoneInfo:
    """Resolve a deterministic zone, falling back to the operator default then UTC."""
    for candidate in (name, config.TIMEZONE, "UTC"):
        if not candidate:
            continue
        try:
            return ZoneInfo(str(candidate))
        except (ZoneInfoNotFoundError, TypeError, ValueError):
            continue
    return ZoneInfo("UTC")


def _studio_zone() -> ZoneInfo:
    row = db.one("SELECT timezone FROM studio WHERE id=?", (STUDIO_ID,))
    configured = row["timezone"] if row else None
    zone = _zone_from_name(configured)
    if configured and zone.key != configured:
        log.error(
            "invalid timezone %r for studio=%s; using %s",
            configured,
            STUDIO_ID,
            zone.key,
        )
    return zone


def _now(zone: ZoneInfo) -> dt.datetime:
    return dt.datetime.now(zone)


def _is_unknown_outcome(error: str | None) -> bool:
    return (error or "").strip().lower().startswith("sms outcome unknown")


def _enqueue_shoot_day_reminders(studio_id: str, reminder_date: str) -> int:
    with db.tx(immediate=True) as con:
        cur = con.execute(
            """INSERT OR IGNORE INTO sms_reminder_intents
               (studio_id, appointment_id, reminder_date, reminder_kind, status, updated_at)
               SELECT a.studio_id, a.id, date(a.starts_at), 'shoot_day',
                      'pending', datetime('now')
               FROM appointments a
               JOIN clients c ON c.id=a.client_id AND c.studio_id=a.studio_id
               WHERE a.studio_id=? AND a.status='confirmed' AND date(a.starts_at)=?
                 AND c.phone IS NOT NULL AND c.phone != ''""",
            (studio_id, reminder_date),
        )
        return cur.rowcount


def _fail_stale_claims() -> int:
    with db.tx(immediate=True) as con:
        cur = con.execute(
            f"""UPDATE sms_reminder_intents
                SET status='failed', error=?, claimed_at=NULL, attempt_token=NULL,
                    updated_at=datetime('now')
                WHERE status='pending'
                  AND claimed_at < datetime('now','{_CLAIM_TIMEOUT}')""",
            (_UNKNOWN_OUTCOME,),
        )
        return cur.rowcount


def _claim_reminder(intent_id: int, studio_id: str):
    attempt_token = security.new_token()
    con = db.connect()
    try:
        cur = con.execute(
            """UPDATE sms_reminder_intents
               SET claimed_at=datetime('now'), attempts=attempts+1,
                   attempt_token=?, updated_at=datetime('now')
               WHERE id=? AND studio_id=? AND status='pending' AND claimed_at IS NULL""",
            (attempt_token, intent_id, studio_id),
        )
        row = None
        if cur.rowcount == 1:
            row = con.execute(
                "SELECT * FROM sms_reminder_intents WHERE id=? AND studio_id=?",
                (intent_id, studio_id),
            ).fetchone()
        con.commit()
        return row
    finally:
        con.close()


def _local_date() -> str:
    return _now(_studio_zone()).date().isoformat()


def _studios_with_reminders() -> list[str]:
    rows = db.all_(
        """SELECT DISTINCT a.studio_id
           FROM appointments a
           JOIN clients c ON c.id=a.client_id AND c.studio_id=a.studio_id
           JOIN studio s ON s.id=a.studio_id
           WHERE s.active=1 AND a.status='confirmed'
             AND c.phone IS NOT NULL AND c.phone != ''
           ORDER BY a.studio_id"""
    )
    return [str(row["studio_id"]) for row in rows]


def _due_candidates(due_dates: list[tuple[str, str]], limit: int):
    candidates = []
    for studio_id, reminder_date in due_dates:
        candidates.extend(
            db.all_(
                """SELECT id, studio_id, created_at FROM sms_reminder_intents
                   WHERE studio_id=? AND status='pending' AND claimed_at IS NULL
                     AND reminder_date=?
                   ORDER BY created_at, id LIMIT ?""",
                (studio_id, reminder_date, limit),
            )
        )
    candidates.sort(key=lambda row: (row["created_at"], row["id"]))
    return candidates[:limit]


def shoot_day_reminders(limit: int = 100) -> int:
    """Persist and send each tenant shoot-day reminder once per appointment/date."""
    if not configured():
        return 0
    from . import tenant

    original_studio = tenant.get_studio_id()
    sent = 0
    try:
        due_dates: list[tuple[str, str]] = []
        for studio_id in _studios_with_reminders():
            tenant.set_studio(studio_id)
            reminder_date = _local_date()
            _enqueue_shoot_day_reminders(studio_id, reminder_date)
            due_dates.append((studio_id, reminder_date))
        _fail_stale_claims()
        candidates = _due_candidates(due_dates, max(0, int(limit)))
        for candidate in candidates:
            intent = _claim_reminder(candidate["id"], candidate["studio_id"])
            if not intent:
                continue
            tenant.set_studio(intent["studio_id"])
            attempt_token = intent["attempt_token"]
            provider_accepted = False
            try:
                row = db.one(
                    """SELECT a.title, a.starts_at, c.phone, c.name
                       FROM appointments a
                       JOIN clients c ON c.id=a.client_id AND c.studio_id=a.studio_id
                       WHERE a.id=? AND a.studio_id=? AND a.status='confirmed'
                         AND date(a.starts_at)=?
                         AND c.phone IS NOT NULL AND c.phone != ''""",
                    (intent["appointment_id"], intent["studio_id"], intent["reminder_date"]),
                )
                if not row:
                    with db.tx(immediate=True) as con:
                        con.execute(
                            """UPDATE sms_reminder_intents
                               SET status='canceled', claimed_at=NULL, attempt_token=NULL,
                                   error='appointment is no longer due',
                                   updated_at=datetime('now')
                               WHERE id=? AND studio_id=? AND status='pending'
                                 AND attempt_token=?""",
                            (intent["id"], intent["studio_id"], attempt_token),
                        )
                    continue
                from .tenant import get_site_name

                when = row["starts_at"][11:16] if row["starts_at"] else "today"
                title = row["title"]
                body = (
                    f"Reminder: {get_site_name()} shoot at {when} — {title}. "
                    "Reply if you need to reschedule."
                )
                if not send(to_phone=row["phone"], body=body):
                    raise RuntimeError("SMS provider did not confirm delivery")
                provider_accepted = True
                with db.tx() as con:
                    updated = con.execute(
                        """UPDATE sms_reminder_intents
                           SET status=?, sent_at=CURRENT_TIMESTAMP, error=NULL,
                               claimed_at=NULL, attempt_token=NULL,
                               updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND studio_id=? AND status=? AND attempt_token=?""",
                        (
                            "sent",
                            intent["id"],
                            intent["studio_id"],
                            "pending",
                            attempt_token,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise RuntimeError("SMS reminder claim was lost after provider acceptance")
                sent += 1
            except Exception as exc:
                error = str(exc)[:500]
                if provider_accepted:
                    error = f"{_UNKNOWN_OUTCOME} {error}"[:500]
                with db.tx(immediate=True) as con:
                    con.execute(
                        """UPDATE sms_reminder_intents
                           SET status=?, error=?, claimed_at=NULL, attempt_token=NULL,
                               updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND studio_id=? AND status=? AND attempt_token=?""",
                        (
                            "failed",
                            error,
                            intent["id"],
                            intent["studio_id"],
                            "pending",
                            attempt_token,
                        ),
                    )
                log.error("shoot-day reminder %s failed: %s", intent["id"], exc)
    finally:
        tenant.set_studio(original_studio)
    return sent


def list_reminder_intents(limit: int = 30):
    return db.all_(
        """SELECT r.*, a.title, a.starts_at, c.name AS client_name, c.phone,
                  CASE WHEN r.status='failed' AND r.claimed_at IS NULL
                         AND r.attempt_token IS NULL
                         AND lower(COALESCE(r.error,'')) LIKE 'sms outcome unknown%'
                       THEN 1 ELSE 0 END AS reconcile_ready
           FROM sms_reminder_intents r
           JOIN appointments a ON a.id=r.appointment_id AND a.studio_id=r.studio_id
           LEFT JOIN clients c ON c.id=a.client_id AND c.studio_id=a.studio_id
           WHERE r.studio_id=?
           ORDER BY r.created_at DESC, r.id DESC LIMIT ?""",
        (STUDIO_ID, limit),
    )


def retry_reminder(intent_id: int) -> bool:
    with db.tx(immediate=True) as con:
        cur = con.execute(
            """UPDATE sms_reminder_intents
               SET status=?, error=NULL, claimed_at=NULL, attempt_token=NULL,
                   updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND studio_id=? AND status=?
                 AND claimed_at IS NULL AND attempt_token IS NULL
                 AND lower(COALESCE(error,'')) NOT LIKE 'sms outcome unknown%'""",
            ("pending", intent_id, STUDIO_ID, "failed"),
        )
        if cur.rowcount:
            db.audit("admin", "sms_reminder.retry", f"intent={intent_id}")
        return cur.rowcount == 1


def reconcile_reminder(intent_id: int, *, delivered: bool) -> None:
    """Record a Twilio-verified outcome without sending another SMS."""
    studio_id = str(STUDIO_ID)
    with db.tx(immediate=True) as con:
        row = con.execute(
            """SELECT r.*, a.title, a.starts_at, c.phone
               FROM sms_reminder_intents r
               JOIN appointments a ON a.id=r.appointment_id AND a.studio_id=r.studio_id
               LEFT JOIN clients c ON c.id=a.client_id AND c.studio_id=a.studio_id
               WHERE r.id=? AND r.studio_id=?""",
            (intent_id, studio_id),
        ).fetchone()
        if not row:
            raise SmsReminderNotFound("SMS reminder intent not found")
        if row["claimed_at"] is not None or row["attempt_token"] is not None:
            raise SmsReminderReconciliationConflict("SMS reminder still has an active claim")
        if row["status"] != "failed" or not _is_unknown_outcome(row["error"]):
            raise SmsReminderReconciliationConflict(
                "only unknown SMS reminder outcomes can be reconciled"
            )

        if delivered:
            cur = con.execute(
                """UPDATE sms_reminder_intents
                   SET status='sent', sent_at=COALESCE(sent_at,CURRENT_TIMESTAMP),
                       error=NULL, claimed_at=NULL, attempt_token=NULL,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND studio_id=? AND status='failed' AND error=?
                     AND claimed_at IS NULL AND attempt_token IS NULL""",
                (intent_id, studio_id, row["error"]),
            )
            if cur.rowcount != 1:
                raise SmsReminderReconciliationConflict("SMS reconciliation state changed")
            if row["phone"]:
                from .tenant import get_site_name

                when = row["starts_at"][11:16] if row["starts_at"] else "today"
                body = (
                    f"Reminder: {get_site_name()} shoot at {when} — {row['title']}. "
                    "Reply if you need to reschedule."
                )
                con.execute(
                    """INSERT INTO sms_log (studio_id, to_phone, body, status)
                       VALUES (?,?,?,'sent')""",
                    (studio_id, row["phone"], body),
                )
            action = "sms_reminder.reconcile.delivered"
        else:
            cur = con.execute(
                """UPDATE sms_reminder_intents
                   SET error=?, claimed_at=NULL, attempt_token=NULL,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND studio_id=? AND status='failed' AND error=?
                     AND claimed_at IS NULL AND attempt_token IS NULL""",
                (_NOT_DELIVERED, intent_id, studio_id, row["error"]),
            )
            if cur.rowcount != 1:
                raise SmsReminderReconciliationConflict("SMS reconciliation state changed")
            action = "sms_reminder.reconcile.not_delivered"
        db.audit("admin", action, f"intent={intent_id}")
