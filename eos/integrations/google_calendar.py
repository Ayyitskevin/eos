"""Google Calendar 2-way sync — push appointments, pull external events."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import urllib.parse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .. import config, db, integration_events, jobs, oauth_store, security, studio
from ..vocab import STUDIO_ID

log = logging.getLogger("eos.integrations.google")

SCOPES = "https://www.googleapis.com/auth/calendar.events"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PROVIDER = "google"
_STATE_MAX_AGE = 600
_RECONCILE_AFTER_MINUTES = 15


class GoogleSyncOutcomeUnknown(RuntimeError):
    """A provider call may have completed but its local commit is unconfirmed."""


class GoogleIntentNotFound(LookupError):
    """The requested intent does not belong to the active studio."""


class GoogleReconciliationConflict(RuntimeError):
    """The intent cannot safely be reconciled in its current state."""


class GoogleAvailabilityUnavailable(RuntimeError):
    """Google free/busy could not be proven, so public availability must close."""


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
    row = studio.get_studio()
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


def _localize_wall_time(value: dt.datetime, zone: ZoneInfo) -> dt.datetime | None:
    """Return one unambiguous instant for a local wall time, else fail closed."""
    matches: list[dt.datetime] = []
    for fold in (0, 1):
        candidate = value.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(dt.UTC).astimezone(zone)
        if round_trip.replace(tzinfo=None) == value and round_trip.fold == fold:
            matches.append(candidate)
    return matches[0] if len(matches) == 1 else None


def _appointment_wall_time(raw: str, zone: ZoneInfo) -> tuple[dt.datetime, dt.datetime]:
    wall = dt.datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
    aware = _localize_wall_time(wall, zone)
    if aware is None:
        raise ValueError(f"appointment time {raw[:19]} is invalid or ambiguous in {zone.key}")
    return wall, aware


def _provider_instant(raw: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Google Calendar dateTime must include a UTC offset")
    return value


def _provider_time_as_local_wall(raw: str, zone: ZoneInfo) -> dt.datetime:
    local = _provider_instant(raw).astimezone(zone)
    wall = local.replace(tzinfo=None)
    if _localize_wall_time(wall, zone) is None:
        raise ValueError(f"Google Calendar time {raw} is ambiguous in studio timezone {zone.key}")
    return wall


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="eos-google-oauth")


def is_configured() -> bool:
    return bool(
        config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET and config.GOOGLE_REDIRECT_URI
    )


def is_connected() -> bool:
    return oauth_store.get_connection(PROVIDER) is not None


def is_intended_enabled() -> bool:
    profile = studio.get_profile()
    return bool(profile["google_calendar_enabled"])


def is_enabled() -> bool:
    return is_intended_enabled() and is_connected()


def connect_url() -> str:
    state = _signer().dumps({"studio_id": STUDIO_ID})
    params = {
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": config.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def handle_callback(*, code: str, state: str) -> None:
    try:
        payload = _signer().loads(state, max_age=_STATE_MAX_AGE)
    except BadSignature as e:
        raise ValueError("Invalid OAuth state") from e
    from .. import tenant

    tenant.set_studio(payload["studio_id"])
    data = oauth_store.exchange_code(
        token_url=TOKEN_URL,
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
        code=code,
        redirect_uri=config.GOOGLE_REDIRECT_URI,
    )
    oauth_store.save_tokens(
        PROVIDER,
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_in=data.get("expires_in"),
        scopes=SCOPES,
        account_label="Google Calendar",
    )
    studio.update_profile(google_calendar_enabled=True)
    db.audit("admin", "integration.google.connect", None)


@db.transactional(immediate=True)
def disconnect() -> None:
    oauth_store.delete_connection(PROVIDER)
    studio.update_profile(google_calendar_enabled=False)
    db.audit("admin", "integration.google.disconnect", None)


def _token() -> str | None:
    tok = oauth_store.access_token(PROVIDER)
    if tok:
        return tok
    return oauth_store.refresh_access(
        PROVIDER,
        token_url=TOKEN_URL,
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
    )


def _calendar_id() -> str:
    profile = studio.get_profile()
    return profile["google_calendar_id"] or "primary"


def _event_body(appt) -> dict:
    zone = _studio_zone()
    start_wall, start_aware = _appointment_wall_time(appt["starts_at"], zone)
    if appt["ends_at"]:
        end_wall, end_aware = _appointment_wall_time(appt["ends_at"], zone)
    else:
        end_wall = start_wall + dt.timedelta(minutes=90)
        end_aware = _localize_wall_time(end_wall, zone)
        if end_aware is None:
            raise ValueError(
                f"appointment end {end_wall:%Y-%m-%d %H:%M:%S} is invalid or ambiguous "
                f"in {zone.key}"
            )
    if end_aware <= start_aware:
        raise ValueError("appointment end must be after its start")
    body = {
        "summary": appt["title"],
        "location": appt["location"] or "",
        "start": {"dateTime": start_wall.isoformat(), "timeZone": zone.key},
        "end": {"dateTime": end_wall.isoformat(), "timeZone": zone.key},
    }
    if appt["status"] == "canceled":
        body["status"] = "cancelled"
    return body


def _deterministic_event_id(studio_id: str, appt_id: int) -> str:
    """Return a Google-compatible, tenant-scoped event id."""
    digest = hashlib.sha256(f"{studio_id}\0{appt_id}".encode()).hexdigest()
    return f"eos{digest}"


def _prepare_intent(
    appt_id: int,
    *,
    retry_failed: bool,
) -> tuple[dict, bool] | None:
    """Persist the desired provider state before a job can run."""
    studio_id = str(STUDIO_ID)
    with db.tx(immediate=True) as con:
        appt = con.execute(
            "SELECT * FROM appointments WHERE id=? AND studio_id=?",
            (appt_id, studio_id),
        ).fetchone()
        if not appt or not appt["starts_at"]:
            return None
        action = "delete" if appt["status"] == "canceled" else "upsert"
        payload = (
            "{}"
            if action == "delete"
            else json.dumps(
                _event_body(appt),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        payload_hash = hashlib.sha256(payload.encode()).hexdigest()
        current = con.execute(
            """SELECT * FROM google_calendar_sync_intents
               WHERE studio_id=? AND appointment_id=?""",
            (studio_id, appt_id),
        ).fetchone()
        event_id = (
            (current["event_id"] if current else None)
            or appt["google_event_id"]
            or _deterministic_event_id(studio_id, appt_id)
        )
        if current:
            same_command = (
                current["desired_action"] == action and current["payload_hash"] == payload_hash
            )
            retry_revision = same_command and current["status"] == "failed" and retry_failed
            changed = not same_command or retry_revision
            revision = int(current["revision"]) + (1 if changed else 0)
            status = "pending" if changed else current["status"]
            con.execute(
                """UPDATE google_calendar_sync_intents
                   SET event_id=?, revision=?, desired_action=?, payload=?, payload_hash=?,
                       status=?, last_error=CASE WHEN ? THEN NULL ELSE last_error END,
                       updated_at=datetime('now')
                   WHERE studio_id=? AND appointment_id=?""",
                (
                    event_id,
                    revision,
                    action,
                    payload,
                    payload_hash,
                    status,
                    1 if changed else 0,
                    studio_id,
                    appt_id,
                ),
            )
        else:
            revision = 1
            status = "pending"
            con.execute(
                """INSERT INTO google_calendar_sync_intents
                   (studio_id, appointment_id, event_id, revision, desired_action,
                    payload, payload_hash, provider_bound, status)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    studio_id,
                    appt_id,
                    event_id,
                    revision,
                    action,
                    payload,
                    payload_hash,
                    1 if appt["google_event_id"] else 0,
                    status,
                ),
            )
        if action != "delete" or status != "synced":
            con.execute(
                """UPDATE appointments SET google_event_id=?
                   WHERE id=? AND studio_id=?""",
                (event_id, appt_id, studio_id),
            )
        intent = con.execute(
            """SELECT * FROM google_calendar_sync_intents
               WHERE studio_id=? AND appointment_id=?""",
            (studio_id, appt_id),
        ).fetchone()
        return dict(intent), status == "pending"


def _claim_intent(appt_id: int) -> dict | None:
    studio_id = str(STUDIO_ID)
    with db.tx(immediate=True) as con:
        intent = con.execute(
            """SELECT * FROM google_calendar_sync_intents
               WHERE studio_id=? AND appointment_id=?""",
            (studio_id, appt_id),
        ).fetchone()
        if not intent:
            raise RuntimeError("google sync intent is missing")
        if intent["status"] == "synced":
            return None
        if intent["status"] == "unknown":
            raise GoogleSyncOutcomeUnknown(
                "Google Calendar outcome is unknown; reconcile the deterministic event "
                "before retrying."
            )
        cur = con.execute(
            """UPDATE google_calendar_sync_intents
               SET status='unknown', attempts=attempts+1, last_error=NULL,
                   updated_at=datetime('now')
               WHERE studio_id=? AND appointment_id=? AND revision=?
                 AND status IN ('pending','failed')""",
            (studio_id, appt_id, intent["revision"]),
        )
        if cur.rowcount != 1:
            raise RuntimeError("google sync intent claim was lost")
        claimed = con.execute(
            """SELECT * FROM google_calendar_sync_intents
               WHERE studio_id=? AND appointment_id=?""",
            (studio_id, appt_id),
        ).fetchone()
        return dict(claimed)


def _mark_provider_failure(intent: dict, error: Exception) -> None:
    with db.tx(immediate=True) as con:
        con.execute(
            """UPDATE google_calendar_sync_intents
               SET status='failed', last_error=?, updated_at=datetime('now')
               WHERE studio_id=? AND appointment_id=? AND revision=? AND status='unknown'""",
            (
                str(error)[:500],
                intent["studio_id"],
                intent["appointment_id"],
                intent["revision"],
            ),
        )


def _complete_push(intent: dict) -> None:
    """Commit a confirmed provider outcome with a revision/status CAS."""
    with db.tx(immediate=True) as con:
        current = con.execute(
            """SELECT revision, status FROM google_calendar_sync_intents
               WHERE studio_id=? AND appointment_id=?""",
            (intent["studio_id"], intent["appointment_id"]),
        ).fetchone()
        if (
            not current
            or current["revision"] != intent["revision"]
            or current["status"] != "unknown"
        ):
            raise GoogleSyncOutcomeUnknown(
                "Google Calendar changed while the provider call was in flight."
            )
        if intent["desired_action"] == "delete":
            cur = con.execute(
                """UPDATE appointments
                   SET google_event_id=NULL, google_synced_at=datetime('now')
                   WHERE id=? AND studio_id=? AND google_event_id=?""",
                (intent["appointment_id"], intent["studio_id"], intent["event_id"]),
            )
        else:
            cur = con.execute(
                """UPDATE appointments
                   SET google_event_id=?, google_synced_at=datetime('now')
                   WHERE id=? AND studio_id=? AND google_event_id=?""",
                (
                    intent["event_id"],
                    intent["appointment_id"],
                    intent["studio_id"],
                    intent["event_id"],
                ),
            )
        if cur.rowcount != 1:
            raise GoogleSyncOutcomeUnknown("Google Calendar appointment binding changed.")
        cur = con.execute(
            """UPDATE google_calendar_sync_intents
               SET status='synced', provider_bound=?, last_error=NULL,
                   updated_at=datetime('now')
               WHERE studio_id=? AND appointment_id=? AND revision=? AND status='unknown'""",
            (
                0 if intent["desired_action"] == "delete" else 1,
                intent["studio_id"],
                intent["appointment_id"],
                intent["revision"],
            ),
        )
        if cur.rowcount != 1:
            raise GoogleSyncOutcomeUnknown("Google Calendar sync commit was lost.")


def _record_push_failure(event: str, error: Exception) -> None:
    try:
        integration_events.log_event("google", event, detail=str(error), ok=False)
    except Exception:
        log.exception("google sync failure could not be recorded")


def list_sync_intents(*, limit: int = 50):
    """Return operator-safe sync state for the active studio only."""
    bounded = max(1, min(int(limit), 100))
    return db.all_(
        """SELECT i.appointment_id, a.title AS appointment_title, a.starts_at,
                  i.event_id, i.revision, i.desired_action, i.provider_bound,
                  i.status, i.attempts, i.last_error, i.created_at, i.updated_at,
                  CASE
                    WHEN i.status='unknown'
                     AND i.updated_at <= datetime('now', ?)
                    THEN 1 ELSE 0
                  END AS reconcile_ready
           FROM google_calendar_sync_intents i
           JOIN appointments a
             ON a.id=i.appointment_id AND a.studio_id=i.studio_id
           WHERE i.studio_id=?
           ORDER BY i.updated_at DESC, i.appointment_id DESC
           LIMIT ?""",
        (
            f"-{_RECONCILE_AFTER_MINUTES} minutes",
            str(STUDIO_ID),
            bounded,
        ),
    )


def reconcile_unknown(appt_id: int, *, applied: bool) -> None:
    """Record an operator-verified provider outcome without calling Google."""
    studio_id = str(STUDIO_ID)
    with db.tx(immediate=True) as con:
        intent = con.execute(
            """SELECT i.*, a.google_event_id
               FROM google_calendar_sync_intents i
               JOIN appointments a
                 ON a.id=i.appointment_id AND a.studio_id=i.studio_id
               WHERE i.studio_id=? AND i.appointment_id=?""",
            (studio_id, appt_id),
        ).fetchone()
        if not intent:
            raise GoogleIntentNotFound("Google sync intent not found")
        if intent["status"] != "unknown":
            raise GoogleReconciliationConflict("only unknown Google sync intents can be reconciled")
        stale = con.execute(
            "SELECT 1 WHERE ? <= datetime('now', ?)",
            (
                intent["updated_at"],
                f"-{_RECONCILE_AFTER_MINUTES} minutes",
            ),
        ).fetchone()
        if not stale:
            raise GoogleReconciliationConflict(
                "Google sync is still within its active claim window; check again later"
            )

        if applied:
            if intent["desired_action"] == "delete":
                con.execute(
                    """UPDATE appointments
                       SET google_event_id=NULL, google_synced_at=datetime('now')
                       WHERE id=? AND studio_id=?""",
                    (appt_id, studio_id),
                )
                provider_bound = 0
            else:
                con.execute(
                    """UPDATE appointments
                       SET google_event_id=?, google_synced_at=datetime('now')
                       WHERE id=? AND studio_id=?""",
                    (intent["event_id"], appt_id, studio_id),
                )
                provider_bound = 1
            status = "synced"
            error = None
            action = "integration.google.reconcile.applied"
            event = "push.reconciled_applied"
        else:
            provider_bound = intent["provider_bound"]
            status = "failed"
            error = "Operator confirmed the Google Calendar operation was not applied."
            action = "integration.google.reconcile.not_applied"
            event = "push.reconciled_not_applied"

        cur = con.execute(
            """UPDATE google_calendar_sync_intents
               SET status=?, provider_bound=?, last_error=?, updated_at=datetime('now')
               WHERE studio_id=? AND appointment_id=? AND revision=? AND status='unknown'""",
            (
                status,
                provider_bound,
                error,
                studio_id,
                appt_id,
                intent["revision"],
            ),
        )
        if cur.rowcount != 1:
            raise GoogleReconciliationConflict("Google reconciliation state changed")
        detail = f"appointment={appt_id} revision={intent['revision']}"
        db.audit("admin", action, detail)
        integration_events.log_event("google", event, detail=detail)


def push_appointment(appt_id: int) -> None:
    if not is_intended_enabled():
        return
    prepared = _prepare_intent(appt_id, retry_failed=False)
    if not prepared:
        return
    token = _token()
    if not token:
        raise RuntimeError(f"google calendar token unavailable for {STUDIO_ID}")
    intent = _claim_intent(appt_id)
    if not intent:
        return
    cal_id = urllib.parse.quote(_calendar_id(), safe="")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        event_url = (
            f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}"
            f"/events/{urllib.parse.quote(intent['event_id'], safe='')}"
        )
        if intent["desired_action"] == "delete":
            resp = httpx.delete(event_url, headers=headers, timeout=30)
            if resp.status_code != 404:
                resp.raise_for_status()
        elif intent["provider_bound"]:
            resp = httpx.patch(
                event_url,
                headers=headers,
                json=json.loads(intent["payload"]),
                timeout=30,
            )
            resp.raise_for_status()
        else:
            body = json.loads(intent["payload"])
            body["id"] = intent["event_id"]
            resp = httpx.post(
                f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events",
                headers=headers,
                json=body,
                timeout=30,
            )
            if resp.status_code == 409:
                resp = httpx.patch(
                    event_url,
                    headers=headers,
                    json=json.loads(intent["payload"]),
                    timeout=30,
                )
            resp.raise_for_status()
        if intent["desired_action"] != "delete":
            returned_id = resp.json().get("id")
            if returned_id and returned_id != intent["event_id"]:
                raise RuntimeError("Google Calendar returned an unexpected event id")
    except Exception as e:
        log.exception("google push failed appt=%s studio=%s", appt_id, STUDIO_ID)
        _mark_provider_failure(intent, e)
        _record_push_failure("push.failed", e)
        raise
    try:
        _complete_push(intent)
        integration_events.log_event(
            "google",
            "push.synced",
            detail=f"appointment={appt_id} revision={intent['revision']}",
        )
    except Exception as e:
        log.exception(
            "google provider outcome unknown appt=%s studio=%s",
            appt_id,
            STUDIO_ID,
        )
        _record_push_failure("push.unknown", e)
        raise GoogleSyncOutcomeUnknown(
            "Google Calendar accepted the operation but its local commit is unknown."
        ) from e


def pull_changes() -> int:
    if not is_intended_enabled():
        return 0
    token = _token()
    if not token:
        raise RuntimeError(f"google calendar token unavailable for {STUDIO_ID}")
    conn = oauth_store.get_connection(PROVIDER)
    cal_id = urllib.parse.quote(_calendar_id(), safe="")
    headers = {"Authorization": f"Bearer {token}"}
    params: dict = {"singleEvents": "true", "showDeleted": "true"}
    if conn and conn["sync_token"]:
        params["syncToken"] = conn["sync_token"]
    else:
        params["timeMin"] = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    zone = _studio_zone()
    imported = 0
    try:
        resp = httpx.get(
            f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events",
            headers=headers,
            params=params,
            timeout=30,
        )
        if resp.status_code == 410:
            oauth_store.set_sync_token(PROVIDER, None)
            return pull_changes()
        resp.raise_for_status()
        data = resp.json()
        with db.tx(immediate=True):
            for ev in data.get("items", []):
                eid = ev.get("id")
                if not eid:
                    continue
                existing = db.one(
                    "SELECT id FROM appointments WHERE studio_id=? AND google_event_id=?",
                    (STUDIO_ID, eid),
                )
                if ev.get("status") == "cancelled":
                    if existing:
                        db.run(
                            "UPDATE appointments SET status='canceled' WHERE id=? AND studio_id=?",
                            (existing["id"], STUDIO_ID),
                        )
                    continue
                start = ev.get("start", {})
                start_at = start.get("dateTime") or start.get("date")
                if not start_at:
                    continue
                if "T" in start_at:
                    starts_at = _provider_time_as_local_wall(start_at, zone).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                else:
                    starts_at = f"{start_at} 09:00:00"
                end = ev.get("end", {})
                end_at = end.get("dateTime") or end.get("date")
                ends_at = None
                if end_at and "T" in end_at:
                    ends_at = _provider_time_as_local_wall(end_at, zone).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                title = ev.get("summary") or "Calendar event"
                if existing:
                    db.run(
                        """UPDATE appointments SET title=?, starts_at=?, ends_at=?,
                           location=?, status='confirmed', google_synced_at=datetime('now')
                           WHERE id=? AND studio_id=?""",
                        (
                            title,
                            starts_at,
                            ends_at,
                            ev.get("location") or "",
                            existing["id"],
                            STUDIO_ID,
                        ),
                    )
                else:
                    db.run(
                        """INSERT INTO appointments
                           (studio_id, title, kind, status, starts_at, ends_at, location,
                            token, google_event_id, google_synced_at, external_source)
                           VALUES (?,?,?,?,?,?,?,?,?,datetime('now'),'google')""",
                        (
                            STUDIO_ID,
                            title,
                            "other",
                            "confirmed",
                            starts_at,
                            ends_at,
                            ev.get("location") or "",
                            security.new_token(),
                            eid,
                        ),
                    )
                    imported += 1
            if data.get("nextSyncToken"):
                oauth_store.set_sync_token(PROVIDER, data["nextSyncToken"])
            integration_events.set_sync_status("google", ok=True)
            if imported:
                integration_events.log_event("google", "pull.imported", detail=f"{imported} events")
    except Exception as e:
        log.exception("google pull failed studio=%s", STUDIO_ID)
        integration_events.set_sync_status("google", ok=False, error=str(e))
        integration_events.log_event("google", "pull.failed", detail=str(e), ok=False)
        raise
    return imported


def busy_ranges(*, days: int = 14) -> list[tuple[dt.datetime, dt.datetime]]:
    if not is_intended_enabled():
        return []
    if not is_connected():
        raise GoogleAvailabilityUnavailable(
            "Google Calendar is enabled but its connection is unavailable."
        )
    token = _token()
    if not token:
        raise GoogleAvailabilityUnavailable(
            "Google Calendar is enabled but its access token is unavailable."
        )
    zone = _studio_zone()
    now = _now(zone)
    end = now + dt.timedelta(days=days)
    body = {
        "timeMin": now.isoformat(),
        "timeMax": end.isoformat(),
        "timeZone": zone.key,
        "items": [{"id": _calendar_id()}],
    }
    try:
        resp = httpx.post(
            "https://www.googleapis.com/calendar/v3/freeBusy",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        cal = resp.json().get("calendars", {}).get(_calendar_id(), {})
        ranges: list[tuple[dt.datetime, dt.datetime]] = []
        for block in cal.get("busy", []):
            start_instant = _provider_instant(block["start"])
            end_instant = _provider_instant(block["end"])
            if end_instant <= start_instant:
                raise ValueError("Google Calendar returned a non-positive busy range")
            start_wall = start_instant.astimezone(zone).replace(tzinfo=None)
            end_wall = end_instant.astimezone(zone).replace(tzinfo=None)
            if end_wall <= start_wall:
                # Naive local storage cannot encode a fold. Conservatively block the
                # elapsed interval instead of collapsing a fall-back range to zero.
                end_wall = start_wall + (end_instant - start_instant)
            ranges.append((start_wall, end_wall))
        return ranges
    except Exception as exc:
        log.exception("google freebusy failed studio=%s", STUDIO_ID)
        raise GoogleAvailabilityUnavailable(
            "Google Calendar availability could not be verified."
        ) from exc


def enqueue_push(appt_id: int) -> None:
    if not is_intended_enabled():
        return
    prepared = _prepare_intent(appt_id, retry_failed=True)
    if not prepared:
        return
    intent, should_enqueue = prepared
    if should_enqueue:
        jobs.enqueue(
            "google_calendar_push",
            {"studio_id": str(STUDIO_ID), "appointment_id": appt_id},
            idempotency_key=f"google-calendar:{appt_id}:r{intent['revision']}",
        )


def sweep_all() -> int:
    total = 0
    rows = db.all_(
        """SELECT sp.studio_id FROM studio_profiles sp
           JOIN studio_oauth o ON o.studio_id=sp.studio_id AND o.provider=?
           WHERE sp.google_calendar_enabled=1""",
        (PROVIDER,),
    )
    from .. import tenant

    original_studio = tenant.get_studio_id()
    try:
        for row in rows:
            tenant.set_studio(row["studio_id"])
            total += pull_changes()
    finally:
        tenant.set_studio(original_studio)
    return total
