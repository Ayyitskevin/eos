"""Durable outbound webhooks for booking, delivery, and payment events."""

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from . import db, security, tenant
from .vocab import STUDIO_ID

log = logging.getLogger("eos.webhooks")
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="eos-hook")
_TIMEOUT = 10.0
_CLAIM_TIMEOUT = "-15 minutes"
_UNKNOWN_OUTCOME = "Delivery outcome unknown; verify the endpoint before retrying."
_NOT_DELIVERED = "Operator confirmed the webhook was not delivered."

EVENTS = ("booking.created", "listing.delivered", "invoice.paid")


class WebhookDeliveryNotFound(LookupError):
    """The requested delivery does not belong to the active studio."""


class WebhookDeliveryReconciliationConflict(RuntimeError):
    """The delivery cannot be safely reconciled in its current state."""


def _is_unknown_outcome(error: str | None) -> bool:
    return (error or "").strip().lower().startswith(_UNKNOWN_OUTCOME.lower())


def _canonical_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _stable_event_key(event: str, payload: dict, explicit: str | None) -> str:
    if explicit:
        key = explicit.strip()
    else:
        digest = hashlib.sha256(_canonical_json(payload).encode()).hexdigest()[:32]
        key = f"{event}:{digest}"
    if not key or len(key) > 200:
        raise ValueError("invalid webhook event key")
    return key


def list_subscriptions():
    return db.all_(
        "SELECT * FROM webhook_subscriptions WHERE studio_id=? ORDER BY id DESC",
        (str(STUDIO_ID),),
    )


def list_deliveries(limit: int = 30):
    return db.all_(
        """SELECT d.*, s.label AS subscription_label, s.url,
                  CASE WHEN d.status='failed' AND d.claimed_at IS NULL
                         AND d.attempt_token IS NULL
                         AND lower(COALESCE(d.error,'')) LIKE
                             'delivery outcome unknown;%'
                       THEN 1 ELSE 0 END AS reconcile_ready
           FROM webhook_deliveries d
           LEFT JOIN webhook_subscriptions s
             ON s.id=d.subscription_id AND s.studio_id=d.studio_id
           WHERE d.studio_id=?
           ORDER BY d.created_at DESC, d.id DESC LIMIT ?""",
        (str(STUDIO_ID), limit),
    )


def _address_is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return _address_is_public(address.ipv4_mapped)
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_reserved
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_unspecified
    )


def _canonical_host(host: str) -> tuple[str, ipaddress.IPv4Address | ipaddress.IPv6Address | None]:
    if not host or "%" in host:
        raise ValueError("webhook URL has an invalid host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            ascii_host = host.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("webhook URL has an invalid host") from exc
        if not ascii_host or len(ascii_host) > 253:
            raise ValueError("webhook URL has an invalid host")
        for label in ascii_host.split("."):
            if (
                not label
                or len(label) > 63
                or not label[0].isalnum()
                or not label[-1].isalnum()
                or any(not (ch.isalnum() or ch == "-") for ch in label)
            ):
                raise ValueError("webhook URL has an invalid host")
        return ascii_host, None
    return str(address), address


def _resolve_public_addresses(
    host: str,
    port: int,
    literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    if literal is not None:
        addresses = [literal]
    else:
        try:
            answers = socket.getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except OSError as exc:
            raise ValueError("webhook host could not be resolved") from exc
        addresses = []
        for answer in answers:
            try:
                addresses.append(ipaddress.ip_address(answer[4][0]))
            except (IndexError, TypeError, ValueError) as exc:
                raise ValueError("webhook host returned an invalid address") from exc
    if not addresses:
        raise ValueError("webhook host did not resolve to an address")
    if any(not _address_is_public(address) for address in addresses):
        raise ValueError("webhook host resolves to a non-public address")
    return tuple(dict.fromkeys(addresses))


def _validated_target(url: str):
    """Validate an HTTPS webhook target and every currently resolved address."""
    endpoint = url.strip()
    if not endpoint or any(ch.isspace() or ord(ch) < 32 or ch == "\\" for ch in endpoint):
        raise ValueError("webhook URL is malformed")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError("webhook URL is malformed") from exc
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError("webhook URL must be https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("webhook URL must not contain credentials")
    if parsed.fragment:
        raise ValueError("webhook URL must not contain a fragment")
    if not 1 <= port <= 65535:
        raise ValueError("webhook URL has an invalid port")
    host, literal = _canonical_host(parsed.hostname or "")
    addresses = _resolve_public_addresses(host, port, literal)
    return endpoint, host, port, addresses


def validate_url(url: str) -> str:
    """Validate an HTTPS webhook target and every currently resolved address."""
    endpoint, _host, _port, _addresses = _validated_target(url)
    return endpoint


def _pinned_url(endpoint: str, address, port: int) -> str:
    parsed = urlsplit(endpoint)
    address_text = str(address)
    netloc = f"[{address_text}]" if isinstance(address, ipaddress.IPv6Address) else address_text
    if port != 443:
        netloc = f"{netloc}:{port}"
    return parsed._replace(netloc=netloc).geturl()


def _post_pinned(
    endpoint: str,
    *,
    host: str,
    port: int,
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    body: bytes,
    headers: dict[str, str],
):
    """Connect to the validated IP while retaining the original Host and TLS SNI."""
    host_header = f"[{host}]" if ":" in host else host
    if port != 443:
        host_header = f"{host_header}:{port}"
    request_headers = {**headers, "Host": host_header}
    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        with client.stream(
            "POST",
            _pinned_url(endpoint, address, port),
            content=body,
            headers=request_headers,
            timeout=_TIMEOUT,
            follow_redirects=False,
            extensions={"sni_hostname": host},
        ) as response:
            return response.status_code


def create_subscription(*, label: str, url: str, events: list[str]) -> int:
    from . import plan_limits

    plan_limits.check_webhook(current_count=len(list_subscriptions()))
    url = validate_url(url)
    ev = [e for e in events if e in EVENTS]
    if not ev:
        ev = list(EVENTS)
    secret = security.new_token()
    wid = db.run(
        """INSERT INTO webhook_subscriptions (studio_id, label, url, secret, events)
           VALUES (?,?,?,?,?)""",
        (str(STUDIO_ID), label.strip() or "Webhook", url, secret, json.dumps(ev)),
    )
    db.audit("admin", "webhook.create", f"id={wid}")
    return wid


def delete_subscription(sub_id: int) -> None:
    db.run("DELETE FROM webhook_subscriptions WHERE id=? AND studio_id=?", (sub_id, STUDIO_ID))


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _wake_delivery(delivery_id: int) -> None:
    owner = db.one(
        """SELECT studio_id FROM webhook_deliveries
           WHERE id=? AND studio_id IS NOT NULL""",
        (delivery_id,),
    )
    if owner:
        _pool.submit(process_delivery, delivery_id, studio_id=owner["studio_id"])


def _persist_delivery(
    sub_id: int,
    studio_id: str,
    event: str,
    payload: dict,
    event_key: str,
    *,
    wake: bool,
) -> int:
    with db.tx() as con:
        cur = con.execute(
            """INSERT OR IGNORE INTO webhook_deliveries
               (subscription_id, studio_id, event, status, event_key, payload, updated_at)
               VALUES (?,?,?,'pending',?,?,datetime('now'))""",
            (sub_id, studio_id, event, event_key, _canonical_json(payload)),
        )
        if cur.rowcount:
            delivery_id = cur.lastrowid
            status = "pending"
        else:
            existing = con.execute(
                """SELECT id, status FROM webhook_deliveries
                   WHERE subscription_id=? AND event_key=?""",
                (sub_id, event_key),
            ).fetchone()
            if not existing:
                raise RuntimeError("webhook delivery deduplication failed")
            delivery_id = existing["id"]
            status = existing["status"]
        if wake and status == "pending":
            db.after_commit(lambda did=delivery_id: _wake_delivery(did))
        return delivery_id


def _claim_delivery(delivery_id: int, studio_id: str):
    attempt_token = security.new_token()
    con = db.connect()
    try:
        cur = con.execute(
            """UPDATE webhook_deliveries
                SET claimed_at=datetime('now'), attempts=attempts+1,
                    attempt_token=?, updated_at=datetime('now')
                WHERE id=? AND studio_id=? AND status='pending'
                  AND claimed_at IS NULL""",
            (attempt_token, delivery_id, studio_id),
        )
        if cur.rowcount != 1:
            con.commit()
            return None
        row = con.execute(
            """SELECT d.*, s.url, s.secret, s.active
               FROM webhook_deliveries d
               LEFT JOIN webhook_subscriptions s
                 ON s.id=d.subscription_id AND s.studio_id=d.studio_id
               WHERE d.id=? AND d.studio_id=?""",
            (delivery_id, studio_id),
        ).fetchone()
        con.commit()
        return row
    finally:
        con.close()


def _delivery_timestamp(created_at: str) -> str:
    return f"{created_at.replace(' ', 'T')}Z"


def _attempt_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def process_delivery(
    delivery_id: int,
    *,
    studio_id: str | None = None,
    url: str | None = None,
    secret: str | None = None,
) -> bool:
    claim_studio = studio_id or tenant.get_studio_id()
    delivery = _claim_delivery(delivery_id, claim_studio)
    if not delivery:
        return False
    previous_studio = tenant.get_studio_id()
    studio_id = delivery["studio_id"]
    attempt_token = delivery["attempt_token"]
    tenant.set_studio(studio_id)
    response_status = None
    provider_dispatched = False
    try:
        endpoint = url or delivery["url"] or ""
        signing_secret = secret or delivery["secret"]
        if not signing_secret or (url is None and not delivery["active"]):
            raise RuntimeError("webhook subscription is missing or inactive")
        endpoint, endpoint_host, endpoint_port, addresses = _validated_target(endpoint)
        try:
            stored = json.loads(delivery["payload"] or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("stored webhook payload is invalid") from exc
        created_at = _delivery_timestamp(delivery["created_at"])
        timestamp = _attempt_timestamp()
        payload = {
            **stored,
            "delivery_id": delivery_id,
            "created_at": created_at,
            "timestamp": timestamp,
            "event_key": delivery["event_key"],
        }
        body = _canonical_json(payload).encode()
        provider_dispatched = True
        response_status = _post_pinned(
            endpoint,
            host=endpoint_host,
            port=endpoint_port,
            address=addresses[0],
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-Eos-Event": delivery["event"],
                "X-Eos-Event-Key": delivery["event_key"],
                "X-Eos-Delivery-Id": str(delivery_id),
                "X-Eos-Timestamp": timestamp,
                "X-Eos-Signature": _sign(signing_secret, body),
            },
        )
        if not 200 <= response_status < 300:
            provider_dispatched = False
            raise RuntimeError(f"HTTP {response_status}")
        with db.tx(immediate=True) as con:
            updated = con.execute(
                """UPDATE webhook_deliveries
                   SET status='ok', error=NULL, response_status=?, claimed_at=NULL,
                       attempt_token=NULL, updated_at=datetime('now')
                   WHERE id=? AND studio_id=? AND status='pending'
                     AND attempt_token=?""",
                (response_status, delivery_id, studio_id, attempt_token),
            )
            if updated.rowcount != 1:
                raise RuntimeError("webhook claim was lost after provider acceptance")
        return True
    except Exception as exc:
        error = str(exc)[:500]
        if provider_dispatched:
            error = f"{_UNKNOWN_OUTCOME} {error}"[:500]
        with db.tx(immediate=True) as con:
            con.execute(
                """UPDATE webhook_deliveries
                   SET status='failed', error=?, response_status=?, claimed_at=NULL,
                       attempt_token=NULL, updated_at=datetime('now')
                   WHERE id=? AND studio_id=? AND status='pending'
                     AND attempt_token=?""",
                (error, response_status, delivery_id, studio_id, attempt_token),
            )
        log.error("webhook delivery %s failed: %s", delivery_id, exc)
        return False
    finally:
        tenant.set_studio(previous_studio)


def process_pending(limit: int = 20) -> int:
    with db.tx(immediate=True) as con:
        con.execute(
            f"""UPDATE webhook_deliveries
                SET status='failed', error=?, claimed_at=NULL, attempt_token=NULL,
                    updated_at=datetime('now')
                WHERE status='pending'
                  AND claimed_at < datetime('now','{_CLAIM_TIMEOUT}')""",
            (_UNKNOWN_OUTCOME,),
        )
    pending = db.all_(
        """SELECT id, studio_id FROM webhook_deliveries
            WHERE status='pending'
              AND claimed_at IS NULL
            ORDER BY created_at, id LIMIT ?""",
        (limit,),
    )
    return sum(1 for row in pending if process_delivery(row["id"], studio_id=row["studio_id"]))


def retry_delivery(delivery_id: int) -> bool:
    from fastapi import HTTPException

    row = db.one(
        "SELECT id FROM webhook_deliveries WHERE id=? AND studio_id=?",
        (delivery_id, STUDIO_ID),
    )
    if not row:
        raise HTTPException(status_code=404)
    with db.tx() as con:
        cur = con.execute(
            """UPDATE webhook_deliveries
               SET status='pending', error=NULL, response_status=NULL, claimed_at=NULL,
                   attempt_token=NULL, updated_at=datetime('now')
               WHERE id=? AND studio_id=? AND status='failed'
                 AND claimed_at IS NULL AND attempt_token IS NULL
                 AND lower(COALESCE(error,'')) NOT LIKE 'delivery outcome unknown;%'""",
            (delivery_id, STUDIO_ID),
        )
        if cur.rowcount:
            db.audit("admin", "webhook.retry", f"delivery={delivery_id}")
            db.after_commit(lambda did=delivery_id: _wake_delivery(did))
        return cur.rowcount == 1


def reconcile_delivery(delivery_id: int, *, delivered: bool) -> None:
    """Record an endpoint-verified outcome without issuing an HTTP request."""
    studio_id = str(STUDIO_ID)
    with db.tx(immediate=True) as con:
        row = con.execute(
            """SELECT * FROM webhook_deliveries
               WHERE id=? AND studio_id=?""",
            (delivery_id, studio_id),
        ).fetchone()
        if not row:
            raise WebhookDeliveryNotFound("webhook delivery not found")
        if row["claimed_at"] is not None or row["attempt_token"] is not None:
            raise WebhookDeliveryReconciliationConflict(
                "webhook delivery still has an active claim"
            )
        if row["status"] != "failed" or not _is_unknown_outcome(row["error"]):
            raise WebhookDeliveryReconciliationConflict(
                "only unknown webhook delivery outcomes can be reconciled"
            )

        if delivered:
            status = "ok"
            error = None
            action = "webhook.reconcile.delivered"
        else:
            status = "failed"
            error = _NOT_DELIVERED
            action = "webhook.reconcile.not_delivered"
        cur = con.execute(
            """UPDATE webhook_deliveries
               SET status=?, error=?, claimed_at=NULL, attempt_token=NULL,
                   updated_at=datetime('now')
               WHERE id=? AND studio_id=? AND status='failed' AND error=?
                 AND claimed_at IS NULL AND attempt_token IS NULL""",
            (status, error, delivery_id, studio_id, row["error"]),
        )
        if cur.rowcount != 1:
            raise WebhookDeliveryReconciliationConflict(
                "webhook delivery reconciliation state changed"
            )
        db.audit("admin", action, f"delivery={delivery_id}")


def _post(sub_id: int, studio_id: str, url: str, secret: str, payload: dict) -> None:
    """Backward-compatible direct delivery helper used by older callers/tests."""
    event = str(payload.get("event", ""))
    normalized = {**payload, "studio_id": payload.get("studio_id", studio_id)}
    event_key = _stable_event_key(event, normalized, None)
    delivery_id = _persist_delivery(
        sub_id,
        studio_id,
        event,
        normalized,
        event_key,
        wake=False,
    )
    process_delivery(delivery_id, studio_id=studio_id, url=url, secret=secret)


def dispatch(
    event: str,
    payload: dict,
    *,
    studio_id: str | None = None,
    event_key: str | None = None,
) -> int:
    """Persist matching delivery intents and wake workers only after commit."""
    sid = studio_id or str(STUDIO_ID)
    subs = db.all_(
        "SELECT * FROM webhook_subscriptions WHERE studio_id=? AND active=1",
        (sid,),
    )
    if not subs:
        return 0
    normalized = {"event": event, "studio_id": sid, "data": payload}
    stable_key = _stable_event_key(event, normalized, event_key)
    queued = 0
    for sub in subs:
        try:
            events = json.loads(sub["events"] or "[]")
        except json.JSONDecodeError:
            events = list(EVENTS)
        if event not in events:
            continue
        _persist_delivery(sub["id"], sid, event, normalized, stable_key, wake=True)
        queued += 1
    return queued
