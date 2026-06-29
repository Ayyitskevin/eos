"""Google Calendar 2-way sync — push appointments, pull external events."""

from __future__ import annotations

import datetime as dt
import logging
import urllib.parse

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


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="eos-google-oauth")


def is_configured() -> bool:
    return bool(
        config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET and config.GOOGLE_REDIRECT_URI
    )


def is_connected() -> bool:
    return oauth_store.get_connection(PROVIDER) is not None


def is_enabled() -> bool:
    profile = studio.get_profile()
    return bool(profile["google_calendar_enabled"]) and is_connected()


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
    start = appt["starts_at"][:19].replace(" ", "T")
    if appt["ends_at"]:
        end = appt["ends_at"][:19].replace(" ", "T")
    else:
        start_dt = dt.datetime.strptime(appt["starts_at"][:19], "%Y-%m-%d %H:%M:%S")
        end = (start_dt + dt.timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%S")
    body = {
        "summary": appt["title"],
        "location": appt["location"] or "",
        "start": {"dateTime": start, "timeZone": config.TIMEZONE},
        "end": {"dateTime": end, "timeZone": config.TIMEZONE},
    }
    if appt["status"] == "canceled":
        body["status"] = "cancelled"
    return body


def push_appointment(appt_id: int) -> None:
    if not is_enabled():
        return
    appt = db.one("SELECT * FROM appointments WHERE id=? AND studio_id=?", (appt_id, STUDIO_ID))
    if not appt or not appt["starts_at"]:
        return
    token = _token()
    if not token:
        log.warning("google calendar: no token for %s", STUDIO_ID)
        return
    cal_id = urllib.parse.quote(_calendar_id(), safe="")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        if appt["status"] == "canceled" and appt["google_event_id"]:
            httpx.delete(
                f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events/{appt['google_event_id']}",
                headers=headers,
                timeout=30,
            )
            db.run(
                """UPDATE appointments SET google_event_id=NULL, google_synced_at=datetime('now')
                   WHERE id=? AND studio_id=?""",
                (appt_id, STUDIO_ID),
            )
            return
        body = _event_body(appt)
        if appt["google_event_id"]:
            resp = httpx.patch(
                f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events/{appt['google_event_id']}",
                headers=headers,
                json=body,
                timeout=30,
            )
        else:
            resp = httpx.post(
                f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events",
                headers=headers,
                json=body,
                timeout=30,
            )
        resp.raise_for_status()
        event_id = resp.json().get("id")
        db.run(
            """UPDATE appointments SET google_event_id=?, google_synced_at=datetime('now')
               WHERE id=? AND studio_id=?""",
            (event_id, appt_id, STUDIO_ID),
        )
    except Exception as e:
        log.exception("google push failed appt=%s studio=%s", appt_id, STUDIO_ID)
        integration_events.log_event("google", "push.failed", detail=str(e), ok=False)


def pull_changes() -> int:
    if not is_enabled():
        return 0
    token = _token()
    if not token:
        return 0
    conn = oauth_store.get_connection(PROVIDER)
    cal_id = urllib.parse.quote(_calendar_id(), safe="")
    headers = {"Authorization": f"Bearer {token}"}
    params: dict = {"singleEvents": "true", "showDeleted": "true"}
    if conn and conn["sync_token"]:
        params["syncToken"] = conn["sync_token"]
    else:
        params["timeMin"] = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
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
                starts_at = start_at[:19].replace("T", " ")
            else:
                starts_at = f"{start_at} 09:00:00"
            end = ev.get("end", {})
            end_at = end.get("dateTime") or end.get("date")
            ends_at = None
            if end_at and "T" in end_at:
                ends_at = end_at[:19].replace("T", " ")
            title = ev.get("summary") or "Calendar event"
            if existing:
                db.run(
                    """UPDATE appointments SET title=?, starts_at=?, ends_at=?, location=?, status='confirmed',
                       google_synced_at=datetime('now') WHERE id=? AND studio_id=?""",
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
                       (studio_id, title, kind, status, starts_at, ends_at, location, token,
                        google_event_id, google_synced_at, external_source)
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
    return imported


def busy_ranges(*, days: int = 14) -> list[tuple[dt.datetime, dt.datetime]]:
    if not is_enabled():
        return []
    token = _token()
    if not token:
        return []
    now = dt.datetime.now()
    end = now + dt.timedelta(days=days)
    body = {
        "timeMin": now.isoformat(),
        "timeMax": end.isoformat(),
        "timeZone": config.TIMEZONE,
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
            b0 = dt.datetime.fromisoformat(block["start"].replace("Z", "+00:00")).replace(
                tzinfo=None
            )
            b1 = dt.datetime.fromisoformat(block["end"].replace("Z", "+00:00")).replace(tzinfo=None)
            ranges.append((b0, b1))
        return ranges
    except Exception:
        log.exception("google freebusy failed studio=%s", STUDIO_ID)
        return []


def enqueue_push(appt_id: int) -> None:
    if is_enabled():
        jobs.enqueue(
            "google_calendar_push", {"studio_id": str(STUDIO_ID), "appointment_id": appt_id}
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

    for row in rows:
        tenant.set_studio(row["studio_id"])
        total += pull_changes()
    return total
