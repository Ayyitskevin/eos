"""Security regressions at public referral, credit, and webhook boundaries."""

import json
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from eos import commerce, db, referrals, security, tenant, webhooks
from eos.routes import site as site_routes
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient


def _client(*, name: str = "Referral Owner", email: str = "owner@example.com") -> int:
    return db.run(
        "INSERT INTO clients (studio_id, name, email) VALUES ('default', ?, ?)",
        (name, email),
    )


def _inquiry(*, client_id: int, email: str = "recipient@example.com") -> int:
    return db.run(
        """INSERT INTO inquiries (studio_id, name, email, client_id)
           VALUES ('default', 'Referral Recipient', ?, ?)""",
        (email, client_id),
    )


def _enable_booking() -> None:
    site_routes.studio.get_profile()
    db.run("UPDATE studio_profiles SET booking_enabled=1 WHERE studio_id='default'")


def _dns_answers(*addresses: str):
    def resolve(_host, port, **_kwargs):
        answers = []
        for address in addresses:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
            answers.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
        return answers

    return resolve


def _delivery(subscription_id: int, *, key: str) -> int:
    return webhooks._persist_delivery(
        subscription_id,
        "default",
        "listing.delivered",
        {"event": "listing.delivered", "studio_id": "default", "data": {"listing_id": 1}},
        key,
        wake=False,
    )


def test_auto_referral_is_high_entropy_finite_and_replay_safe(app_env_http):
    client_id = _client()

    with ThreadPoolExecutor(max_workers=8) as executor:
        rows = list(executor.map(lambda _n: referrals.ensure_for_client(client_id), range(8)))

    assert len({row["id"] for row in rows}) == 1
    assert len({row["code"] for row in rows}) == 1
    row = rows[0]
    assert re.fullmatch(r"REF-[0-9A-F]{32}", row["code"])
    assert row["code"] != f"REFER{client_id}"
    assert row["max_uses"] == referrals.AUTO_REFERRAL_MAX_USES
    assert referrals.ensure_for_client(client_id)["id"] == row["id"]
    assert (
        db.one(
            "SELECT COUNT(*) AS n FROM referral_codes WHERE referrer_client_id=?",
            (client_id,),
        )["n"]
        == 1
    )


def test_auto_referral_last_use_is_atomic_under_concurrency(app_env_http):
    client_id = _client()
    row = referrals.ensure_for_client(client_id)
    db.run(
        "UPDATE referral_codes SET uses=max_uses-1 WHERE id=? AND studio_id='default'",
        (row["id"],),
    )
    barrier = threading.Barrier(2)

    def redeem(_n: int) -> str:
        tenant.set_studio("default")
        barrier.wait()
        try:
            referrals.record_use(row["id"])
        except HTTPException as exc:
            return f"rejected:{exc.status_code}"
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = sorted(executor.map(redeem, range(2)))

    assert results == ["accepted", "rejected:409"]
    final = db.one("SELECT uses, max_uses FROM referral_codes WHERE id=?", (row["id"],))
    assert final["uses"] == final["max_uses"]


def test_referral_rejects_self_redemption_without_side_effects(app_env_http):
    client_id = _client()
    row = referrals.ensure_for_client(client_id)
    inquiry_id = _inquiry(client_id=client_id, email="owner@example.com")

    with pytest.raises(HTTPException, match="Self-referrals") as exc_info:
        referrals.record_use(
            row["id"],
            inquiry_id=inquiry_id,
            referred_client_id=client_id,
        )

    assert exc_info.value.status_code == 409
    assert db.one("SELECT uses FROM referral_codes WHERE id=?", (row["id"],))["uses"] == 0
    assert (
        db.one(
            "SELECT COUNT(*) AS n FROM referral_redemptions WHERE referral_id=?",
            (row["id"],),
        )["n"]
        == 0
    )


def test_referral_reservation_is_replay_safe_and_finalizes_once(app_env_http):
    owner_id = _client()
    recipient_id = _client(name="Recipient", email="recipient@example.com")
    code = referrals.ensure_for_client(owner_id)
    db.run("UPDATE referral_codes SET max_uses=1 WHERE id=?", (code["id"],))
    inquiry_id = _inquiry(client_id=recipient_id)
    other_inquiry_id = _inquiry(
        client_id=_client(name="Other", email="other@example.com"),
        email="other@example.com",
    )
    expires_at = db.one("SELECT datetime('now','+1 hour') AS value")["value"]

    first = referrals.reserve_use(
        code["id"],
        inquiry_id=inquiry_id,
        referred_client_id=recipient_id,
        expires_at=expires_at,
    )
    replay = referrals.reserve_use(
        code["id"],
        inquiry_id=inquiry_id,
        referred_client_id=recipient_id,
        expires_at=expires_at,
    )

    assert replay["id"] == first["id"]
    assert first["status"] == "reserved"
    assert db.one("SELECT uses FROM referral_codes WHERE id=?", (code["id"],))["uses"] == 0
    with pytest.raises(HTTPException, match="no longer available") as exc_info:
        referrals.reserve_use(
            code["id"],
            inquiry_id=other_inquiry_id,
            referred_client_id=None,
            expires_at=expires_at,
        )
    assert exc_info.value.status_code == 409

    assert referrals.finalize_inquiry(inquiry_id) is True
    assert referrals.finalize_inquiry(inquiry_id) is False
    final = db.one(
        """SELECT r.uses, rr.status, rr.finalized_at
           FROM referral_codes r
           JOIN referral_redemptions rr ON rr.referral_id=r.id
           WHERE r.id=? AND rr.inquiry_id=?""",
        (code["id"], inquiry_id),
    )
    assert final["uses"] == 1
    assert final["status"] == "confirmed"
    assert final["finalized_at"]


def test_referral_finalization_accepts_grace_adjusted_payment_cutoff(app_env_http):
    owner_id = _client()
    recipient_id = _client(name="Recipient", email="recipient@example.com")
    code = referrals.ensure_for_client(owner_id)
    inquiry_id = _inquiry(client_id=recipient_id)
    payment_expires_at = db.one("SELECT datetime('now','-5 minutes') AS value")["value"]
    reservation_expires_at = db.one(
        "SELECT datetime(?, ?) AS value",
        (
            payment_expires_at,
            f"+{commerce.PAYMENT_WEBHOOK_GRACE_MINUTES} minutes",
        ),
    )["value"]
    db.run(
        "UPDATE inquiries SET payment_expires_at=? WHERE id=?",
        (payment_expires_at, inquiry_id),
    )

    reserved = referrals.reserve_use(
        code["id"],
        inquiry_id=inquiry_id,
        referred_client_id=recipient_id,
        expires_at=reservation_expires_at,
    )

    assert payment_expires_at < db.one("SELECT datetime('now') AS value")["value"]
    assert reserved["expires_at"] == reservation_expires_at
    assert referrals.finalize_inquiry(inquiry_id) is True
    assert db.one("SELECT uses FROM referral_codes WHERE id=?", (code["id"],))["uses"] == 1


def test_booking_reserves_referral_through_payment_webhook_grace(app_env_http):
    _enable_booking()
    owner_id = _client()
    code = referrals.ensure_for_client(owner_id)
    package = db.one(
        "SELECT id FROM service_packages WHERE studio_id='default' AND active=1 LIMIT 1"
    )
    db.run(
        "UPDATE service_packages SET deposit_cents=5000 WHERE id=?",
        (package["id"],),
    )
    slots = site_routes.scheduling.open_slots()
    assert slots

    result = commerce.create_booking(
        name="Referred Recipient",
        email="booked-recipient@example.com",
        phone="",
        property_address="42 Grace Period Lane",
        package_id=package["id"],
        scheduled_at=slots[0]["value"],
        signer_name="Referred Recipient",
        promo_code=code["code"],
        request_key="referral-grace-integration",
    )

    lifecycle = db.one(
        """SELECT q.status AS inquiry_status, q.payment_expires_at,
                  rr.status AS redemption_status, rr.expires_at,
                  r.uses
           FROM inquiries q
           JOIN referral_redemptions rr
             ON rr.inquiry_id=q.id AND rr.studio_id=q.studio_id
           JOIN referral_codes r
             ON r.id=rr.referral_id AND r.studio_id=rr.studio_id
           WHERE q.id=? AND q.studio_id='default'""",
        (result["inquiry_id"],),
    )
    expected_cutoff = db.one(
        "SELECT datetime(?, ?) AS value",
        (
            lifecycle["payment_expires_at"],
            f"+{commerce.PAYMENT_WEBHOOK_GRACE_MINUTES} minutes",
        ),
    )["value"]
    assert lifecycle["inquiry_status"] == "pending_payment"
    assert lifecycle["redemption_status"] == "reserved"
    assert lifecycle["expires_at"] == expected_cutoff
    assert lifecycle["uses"] == 0


def test_releasing_referral_reservation_restores_capacity(app_env_http):
    owner_id = _client()
    code = referrals.ensure_for_client(owner_id)
    db.run("UPDATE referral_codes SET max_uses=1 WHERE id=?", (code["id"],))
    first_client = _client(name="First", email="first@example.com")
    second_client = _client(name="Second", email="second@example.com")
    first_inquiry = _inquiry(client_id=first_client, email="first@example.com")
    second_inquiry = _inquiry(client_id=second_client, email="second@example.com")
    expires_at = db.one("SELECT datetime('now','+1 hour') AS value")["value"]

    referrals.reserve_use(
        code["id"],
        inquiry_id=first_inquiry,
        referred_client_id=first_client,
        expires_at=expires_at,
    )
    assert referrals.release_inquiry(first_inquiry) is True
    assert referrals.release_inquiry(first_inquiry) is False
    second = referrals.reserve_use(
        code["id"],
        inquiry_id=second_inquiry,
        referred_client_id=second_client,
        expires_at=expires_at,
    )

    assert second["status"] == "reserved"
    assert db.one("SELECT uses FROM referral_codes WHERE id=?", (code["id"],))["uses"] == 0


def test_expired_referral_reservation_cannot_be_finalized(app_env_http):
    owner_id = _client()
    recipient_id = _client(name="Recipient", email="recipient@example.com")
    code = referrals.ensure_for_client(owner_id)
    inquiry_id = _inquiry(client_id=recipient_id)
    expires_at = db.one("SELECT datetime('now','+1 hour') AS value")["value"]
    referrals.reserve_use(
        code["id"],
        inquiry_id=inquiry_id,
        referred_client_id=recipient_id,
        expires_at=expires_at,
    )
    db.run(
        "UPDATE referral_redemptions SET expires_at=datetime('now','-1 minute') WHERE inquiry_id=?",
        (inquiry_id,),
    )

    with pytest.raises(HTTPException, match="expired") as exc_info:
        referrals.finalize_inquiry(inquiry_id)

    assert exc_info.value.status_code == 409
    assert db.one("SELECT uses FROM referral_codes WHERE id=?", (code["id"],))["uses"] == 0
    assert referrals.release_inquiry(inquiry_id) is True


def test_referral_reservation_capacity_is_atomic_under_concurrency(app_env_http):
    owner_id = _client()
    code = referrals.ensure_for_client(owner_id)
    db.run("UPDATE referral_codes SET max_uses=1 WHERE id=?", (code["id"],))
    recipients = [
        _client(name=f"Recipient {index}", email=f"recipient-{index}@example.com")
        for index in range(2)
    ]
    inquiries = [
        _inquiry(client_id=client_id, email=f"recipient-{index}@example.com")
        for index, client_id in enumerate(recipients)
    ]
    expires_at = db.one("SELECT datetime('now','+1 hour') AS value")["value"]
    barrier = threading.Barrier(2)

    def reserve(index: int) -> str:
        tenant.set_studio("default")
        barrier.wait()
        try:
            referrals.reserve_use(
                code["id"],
                inquiry_id=inquiries[index],
                referred_client_id=recipients[index],
                expires_at=expires_at,
            )
        except HTTPException as exc:
            return f"rejected:{exc.status_code}"
        return "reserved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = sorted(executor.map(reserve, range(2)))

    assert results == ["rejected:409", "reserved"]
    assert (
        db.one("SELECT COUNT(*) AS n FROM referral_redemptions WHERE status='reserved'")["n"] == 1
    )
    assert db.one("SELECT uses FROM referral_codes WHERE id=?", (code["id"],))["uses"] == 0


def test_explicit_referral_code_behavior_is_preserved(app_env_http):
    client_id = _client()
    referral_id = referrals.create_code(
        code=" agent-launch ",
        credit_cents=1700,
        referrer_client_id=client_id,
    )

    row = referrals.ensure_for_client(client_id)
    assert row["id"] == referral_id
    assert row["code"] == "AGENT-LAUNCH"
    assert row["credit_cents"] == 1700
    assert row["max_uses"] is None


@pytest.mark.asyncio
async def test_public_referral_context_never_exposes_referrer_pii(app_env_http):
    name = "Private Referral Owner 7E19"
    email = "private-referrer-7e19@example.com"
    client_id = _client(name=name, email=email)
    _enable_booking()
    referrals.create_code(
        code="PRIVATE25",
        credit_cents=2500,
        referrer_client_id=client_id,
        max_uses=3,
    )

    transport = ASGITransport(app=app_env_http)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/book?ref=PRIVATE25")

    assert response.status_code == 200
    assert "Referral code <strong>PRIVATE25</strong> applied" in response.text
    assert name not in response.text
    assert email not in response.text
    assert "referrer_name" not in site_routes._referral_context("PRIVATE25")


@pytest.mark.asyncio
async def test_tenant_storefront_is_hidden_until_activation_complete(app_env_http, monkeypatch):
    monkeypatch.setattr(site_routes.config, "SAAS_MODE", True)
    monkeypatch.setattr(site_routes.config, "BASE_DOMAIN", "eos.test")
    db.run(
        """INSERT INTO studio (id, name, slug, active, signup_verified)
           VALUES ('gate', 'Private Gate Studio', 'gate', 0, 0)"""
    )
    db.run(
        """INSERT INTO studio_profiles (studio_id, headline, published)
           VALUES ('gate', 'Should remain private', 0)"""
    )
    transport = ASGITransport(app=app_env_http)
    async with AsyncClient(transport=transport, base_url="http://gate.eos.test") as client:
        inactive = await client.get("/")
        db.run("UPDATE studio SET active=1 WHERE id='gate'")
        unverified = await client.get("/")
        db.run("UPDATE studio SET signup_verified=1 WHERE id='gate'")
        unpublished = await client.get("/")
        db.run("UPDATE studio_profiles SET published=1 WHERE studio_id='gate'")
        visible = await client.get("/")

    assert [inactive.status_code, unverified.status_code, unpublished.status_code] == [
        404,
        404,
        404,
    ]
    assert all(
        "Should remain private" not in response.text
        for response in (inactive, unverified, unpublished)
    )
    assert visible.status_code == 200
    assert "Should remain private" in visible.text


@pytest.mark.asyncio
async def test_stored_credit_requires_matching_returning_capability(app_env_http, monkeypatch):
    client_id = _client(email="credit-owner@example.com")
    _enable_booking()
    token = "returning-capability-token"
    db.run(
        "UPDATE clients SET portal_token=?, credit_cents=5000 WHERE id=? AND studio_id='default'",
        (token, client_id),
    )
    captured: list[int | None] = []

    def fake_booking(**kwargs):
        captured.append(kwargs["credit_client_id"])
        return {"pay_slug": None, "order_token": f"order-{len(captured)}"}

    monkeypatch.setattr(site_routes.commerce, "create_booking", fake_booking)
    monkeypatch.setattr(site_routes.security, "inquiry_throttled", lambda *_args: False)
    monkeypatch.setattr(site_routes.security, "inquiry_record", lambda *_args: None)
    base_form = {
        "name": "Credit Owner",
        "email": "credit-owner@example.com",
        "phone": "",
        "property_address": "1 Secure Way",
        "package_id": "1",
        "scheduled_at": "2026-08-01T10:00:00",
        "signer_name": "Credit Owner",
        "request_key": "credit-boundary",
    }
    transport = ASGITransport(app=app_env_http)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        no_capability = await client.post("/book", data=base_form, follow_redirects=False)
        with_capability = await client.post(
            "/book",
            data={**base_form, "request_key": "credit-capability", "returning_token": token},
            follow_redirects=False,
        )
        wrong_email = await client.post(
            "/book",
            data={
                **base_form,
                "email": "attacker@example.com",
                "request_key": "credit-wrong-email",
                "returning_token": token,
            },
            follow_redirects=False,
        )
        returning_page = await client.get(f"/book?returning={token}")

    assert [no_capability.status_code, with_capability.status_code, wrong_email.status_code] == [
        303,
        303,
        303,
    ]
    assert captured == [None, client_id, None]
    assert f'name="returning_token" value="{token}"' in returning_page.text


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8/hook",
        "https://user:secret@8.8.8.8/hook",
        "https://127.0.0.1/hook",
        "https://[::1]/hook",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.1/hook",
        "https://224.0.0.1/hook",
        "https://0.0.0.0/hook",
        "https://8.8.8.8/hook#fragment",
        "https://8.8.8.8\\@127.0.0.1/hook",
        "https://bad host/hook",
        "https://",
    ],
)
def test_webhook_url_rejects_malformed_and_non_public_literals(url):
    with pytest.raises(ValueError):
        webhooks.validate_url(url)


@pytest.mark.parametrize(
    "addresses",
    [
        ("10.0.0.8",),
        ("127.0.0.1",),
        ("169.254.169.254",),
        ("224.0.0.1",),
        ("8.8.8.8", "10.0.0.8"),
    ],
)
def test_webhook_configuration_rejects_any_unsafe_dns_answer(app_env_http, monkeypatch, addresses):
    monkeypatch.setattr(webhooks.socket, "getaddrinfo", _dns_answers(*addresses))

    with pytest.raises(ValueError, match="non-public"):
        webhooks.create_subscription(
            label="unsafe",
            url="https://hooks.example.test/eos",
            events=["listing.delivered"],
        )

    assert db.one("SELECT COUNT(*) AS n FROM webhook_subscriptions")["n"] == 0


def test_webhook_configuration_rejects_unresolvable_host(app_env_http, monkeypatch):
    def unresolved(*_args, **_kwargs):
        raise socket.gaierror("not found")

    monkeypatch.setattr(webhooks.socket, "getaddrinfo", unresolved)
    with pytest.raises(ValueError, match="could not be resolved"):
        webhooks.create_subscription(
            label="missing",
            url="https://missing.example.test/eos",
            events=["listing.delivered"],
        )


def test_webhook_send_revalidates_dns_and_blocks_rebinding(app_env_http, monkeypatch):
    answers = iter([_dns_answers("8.8.8.8"), _dns_answers("127.0.0.1")])

    def resolving(*args, **kwargs):
        return next(answers)(*args, **kwargs)

    monkeypatch.setattr(webhooks.socket, "getaddrinfo", resolving)
    subscription_id = webhooks.create_subscription(
        label="rebinding",
        url="https://hooks.example.test/eos",
        events=["listing.delivered"],
    )
    delivery_id = _delivery(subscription_id, key="listing.delivered:rebind")
    posted: list[str] = []
    monkeypatch.setattr(
        webhooks, "_post_pinned", lambda endpoint, **_kwargs: posted.append(endpoint)
    )

    assert webhooks.process_delivery(delivery_id, studio_id="default") is False
    assert posted == []
    delivery = db.one("SELECT status, error FROM webhook_deliveries WHERE id=?", (delivery_id,))
    assert delivery["status"] == "failed"
    assert "non-public" in delivery["error"]


def test_webhook_public_target_is_ip_pinned_without_redirects(app_env_http, monkeypatch):
    dns_calls: list[str] = []

    def resolving(host, port, **kwargs):
        dns_calls.append(host)
        address = "8.8.8.8" if len(dns_calls) <= 2 else "127.0.0.1"
        return _dns_answers(address)(host, port, **kwargs)

    monkeypatch.setattr(webhooks.socket, "getaddrinfo", resolving)
    subscription_id = webhooks.create_subscription(
        label="public",
        url="https://hooks.example.test/eos",
        events=["listing.delivered"],
    )
    delivery_id = _delivery(subscription_id, key="listing.delivered:public")
    calls: list[tuple[str, dict]] = []
    client_options: list[dict] = []

    class Response:
        status_code = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Client:
        def __init__(self, **kwargs):
            client_options.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, _method, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    monkeypatch.setattr(webhooks.httpx, "Client", Client)

    assert webhooks.process_delivery(delivery_id, studio_id="default") is True
    assert dns_calls == ["hooks.example.test", "hooks.example.test"]
    assert calls[0][0] == "https://8.8.8.8/eos"
    assert calls[0][1]["headers"]["Host"] == "hooks.example.test"
    assert calls[0][1]["extensions"] == {"sni_hostname": "hooks.example.test"}
    assert calls[0][1]["follow_redirects"] is False
    assert client_options == [{"trust_env": False, "follow_redirects": False}]
    assert (
        db.one("SELECT status FROM webhook_deliveries WHERE id=?", (delivery_id,))["status"] == "ok"
    )


def test_webhook_redirect_response_is_failed_and_retryable(app_env_http, monkeypatch):
    monkeypatch.setattr(webhooks.socket, "getaddrinfo", _dns_answers("8.8.8.8"))
    monkeypatch.setattr(webhooks, "_wake_delivery", lambda _delivery_id: None)
    subscription_id = webhooks.create_subscription(
        label="redirect",
        url="https://hooks.example.test/redirect",
        events=["listing.delivered"],
    )
    delivery_id = _delivery(subscription_id, key="listing.delivered:redirect")
    monkeypatch.setattr(webhooks, "_post_pinned", lambda _endpoint, **_kwargs: 302)

    assert webhooks.process_delivery(delivery_id, studio_id="default") is False
    failed = db.one(
        "SELECT status, error, response_status FROM webhook_deliveries WHERE id=?",
        (delivery_id,),
    )
    assert failed["status"] == "failed"
    assert failed["response_status"] == 302
    assert failed["error"] == "HTTP 302"
    assert webhooks.retry_delivery(delivery_id) is True
    assert (
        db.one("SELECT status FROM webhook_deliveries WHERE id=?", (delivery_id,))["status"]
        == "pending"
    )


def test_webhook_retry_uses_fresh_signed_timestamp_and_stable_identity(app_env_http, monkeypatch):
    monkeypatch.setattr(webhooks.socket, "getaddrinfo", _dns_answers("8.8.8.8"))
    monkeypatch.setattr(webhooks, "_wake_delivery", lambda _delivery_id: None)
    timestamps = iter(["2026-07-26T15:00:00Z", "2026-07-26T15:01:00Z"])
    monkeypatch.setattr(webhooks, "_attempt_timestamp", lambda: next(timestamps))
    subscription_id = webhooks.create_subscription(
        label="retry",
        url="https://hooks.example.test/retry",
        events=["listing.delivered"],
    )
    delivery_id = _delivery(subscription_id, key="listing.delivered:retry")
    statuses = iter([500, 204])
    calls: list[dict] = []

    class Response:
        def __init__(self):
            self.status_code = next(statuses)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, _method, _url, **kwargs):
            calls.append(kwargs)
            return Response()

    monkeypatch.setattr(webhooks.httpx, "Client", Client)

    assert webhooks.process_delivery(delivery_id, studio_id="default") is False
    assert webhooks.retry_delivery(delivery_id) is True
    assert webhooks.process_delivery(delivery_id, studio_id="default") is True

    payloads = [json.loads(call["content"]) for call in calls]
    assert [payload["timestamp"] for payload in payloads] == [
        "2026-07-26T15:00:00Z",
        "2026-07-26T15:01:00Z",
    ]
    assert payloads[0]["created_at"] == payloads[1]["created_at"]
    assert payloads[0]["delivery_id"] == payloads[1]["delivery_id"] == delivery_id
    assert payloads[0]["event_key"] == payloads[1]["event_key"] == "listing.delivered:retry"
    assert [call["headers"]["X-Eos-Timestamp"] for call in calls] == [
        "2026-07-26T15:00:00Z",
        "2026-07-26T15:01:00Z",
    ]
    delivery = db.one("SELECT status, attempts FROM webhook_deliveries WHERE id=?", (delivery_id,))
    assert delivery["status"] == "ok"
    assert delivery["attempts"] == 2


@pytest.mark.parametrize("old_outcome", ["success", "failure"])
def test_webhook_stale_worker_cannot_overwrite_new_retry(app_env_http, monkeypatch, old_outcome):
    monkeypatch.setattr(webhooks.socket, "getaddrinfo", _dns_answers("8.8.8.8"))
    monkeypatch.setattr(webhooks, "_wake_delivery", lambda _delivery_id: None)
    subscription_id = webhooks.create_subscription(
        label="attempt-fencing",
        url="https://hooks.example.test/fenced",
        events=["listing.delivered"],
    )
    delivery_id = _delivery(subscription_id, key="listing.delivered:attempt-fencing")
    attempt_tokens: list[str] = []

    def post(*_args, **_kwargs):
        row = db.one("SELECT * FROM webhook_deliveries WHERE id=?", (delivery_id,))
        attempt_tokens.append(row["attempt_token"])
        if len(attempt_tokens) == 1:
            db.run(
                """UPDATE webhook_deliveries
                   SET claimed_at='2000-01-01 00:00:00' WHERE id=?""",
                (delivery_id,),
            )
            assert webhooks.process_pending() == 0
            webhooks.reconcile_delivery(delivery_id, delivered=False)
            assert webhooks.retry_delivery(delivery_id) is True
            assert webhooks.process_delivery(delivery_id, studio_id="default") is True
            if old_outcome == "failure":
                raise TimeoutError("old webhook worker failed late")
        return 204

    monkeypatch.setattr(webhooks, "_post_pinned", post)

    assert webhooks.process_delivery(delivery_id, studio_id="default") is False
    row = db.one("SELECT * FROM webhook_deliveries WHERE id=?", (delivery_id,))
    assert row["status"] == "ok"
    assert row["response_status"] == 204
    assert row["attempts"] == 2
    assert row["claimed_at"] is None
    assert row["attempt_token"] is None
    assert row["error"] is None
    assert len(set(attempt_tokens)) == 2


def test_stale_webhook_claim_fails_closed_without_automatic_replay(app_env_http, monkeypatch):
    monkeypatch.setattr(webhooks.socket, "getaddrinfo", _dns_answers("8.8.8.8"))
    subscription_id = webhooks.create_subscription(
        label="stale-claim",
        url="https://hooks.example.test/stale",
        events=["listing.delivered"],
    )
    delivery_id = _delivery(subscription_id, key="listing.delivered:stale-claim")
    db.run(
        """UPDATE webhook_deliveries
           SET claimed_at=datetime('now','-30 minutes') WHERE id=?""",
        (delivery_id,),
    )
    posted: list[int] = []
    monkeypatch.setattr(
        webhooks,
        "_post_pinned",
        lambda *_args, **_kwargs: posted.append(204) or 204,
    )

    assert webhooks.process_pending() == 0
    assert webhooks.process_pending() == 0

    delivery = db.one(
        "SELECT status, attempts, claimed_at, error FROM webhook_deliveries WHERE id=?",
        (delivery_id,),
    )
    assert dict(delivery) == {
        "status": "failed",
        "attempts": 0,
        "claimed_at": None,
        "error": "Delivery outcome unknown; verify the endpoint before retrying.",
    }
    assert posted == []


def test_webhook_provider_success_then_local_failure_requires_reconciliation(
    app_env_http, monkeypatch
):
    monkeypatch.setattr(webhooks.socket, "getaddrinfo", _dns_answers("8.8.8.8"))
    monkeypatch.setattr(webhooks, "_wake_delivery", lambda _delivery_id: None)
    subscription_id = webhooks.create_subscription(
        label="accepted-before-local-failure",
        url="https://hooks.example.test/accepted",
        events=["listing.delivered"],
    )
    delivery_id = _delivery(subscription_id, key="listing.delivered:accepted")
    provider_calls: list[int] = []
    monkeypatch.setattr(
        webhooks,
        "_post_pinned",
        lambda *_args, **_kwargs: provider_calls.append(204) or 204,
    )
    db.run(
        """CREATE TRIGGER fail_webhook_ok_transition
           BEFORE UPDATE OF status ON webhook_deliveries
           WHEN NEW.id=OLD.id AND NEW.status='ok'
           BEGIN
             SELECT RAISE(ABORT, 'injected local persistence failure');
           END"""
    )

    assert webhooks.process_delivery(delivery_id, studio_id="default") is False
    failed = db.one(
        "SELECT status, response_status, error FROM webhook_deliveries WHERE id=?",
        (delivery_id,),
    )
    assert failed["status"] == "failed"
    assert failed["response_status"] == 204
    assert "Delivery outcome unknown" in failed["error"]
    assert "injected local persistence failure" in failed["error"]
    assert webhooks.process_pending() == 0
    assert provider_calls == [204]

    db.run("DROP TRIGGER fail_webhook_ok_transition")
    assert webhooks.retry_delivery(delivery_id) is False
    webhooks.reconcile_delivery(delivery_id, delivered=True)
    assert (
        db.one("SELECT status FROM webhook_deliveries WHERE id=?", (delivery_id,))["status"] == "ok"
    )
    assert provider_calls == [204]


def test_webhook_transport_timeout_is_an_unknown_outcome(app_env_http, monkeypatch):
    monkeypatch.setattr(webhooks.socket, "getaddrinfo", _dns_answers("8.8.8.8"))
    subscription_id = webhooks.create_subscription(
        label="transport-timeout",
        url="https://hooks.example.test/timeout",
        events=["listing.delivered"],
    )
    delivery_id = _delivery(subscription_id, key="listing.delivered:timeout")
    monkeypatch.setattr(
        webhooks,
        "_post_pinned",
        Mock(side_effect=TimeoutError("response lost after dispatch")),
    )

    assert webhooks.process_delivery(delivery_id, studio_id="default") is False

    failed = db.one(
        "SELECT status, response_status, error FROM webhook_deliveries WHERE id=?",
        (delivery_id,),
    )
    assert failed["status"] == "failed"
    assert failed["response_status"] is None
    assert "Delivery outcome unknown" in failed["error"]
    assert "response lost after dispatch" in failed["error"]


@pytest.mark.asyncio
async def test_booking_and_admin_capability_pages_are_not_cacheable(app_env_http):
    _enable_booking()
    client_id = _client(name="Returning Agent", email="returning@example.com")
    db.run(
        "UPDATE clients SET portal_token='returning-capability' WHERE id=?",
        (client_id,),
    )
    transport = ASGITransport(app=app_env_http)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        booking = await client.get("/book?returning=returning-capability")
        admin = await client.get("/admin/login")

    assert booking.status_code == 200
    assert "Returning Agent" in booking.text
    assert booking.headers["cache-control"] == "private, no-store"
    assert admin.headers["cache-control"] == "private, no-store"


def test_new_gallery_pins_are_six_digits(app_env_http):
    pins = {security.new_pin() for _ in range(50)}
    assert all(re.fullmatch(r"[0-9]{6}", pin) for pin in pins)


@pytest.mark.asyncio
async def test_gallery_pin_lockout_is_gallery_wide_not_per_ip(app_env_http):
    db.run(
        """INSERT INTO galleries (studio_id, slug, title, pin, delivery_token, published)
           VALUES ('default', 'pin-lockout-gallery', 'PIN Gallery', '654321', 'tok-pin', 1)"""
    )
    transport = ASGITransport(app=app_env_http)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Distributed guessing: each source IP stays far below the per-IP limit.
        for i in range(security.config.GALLERY_PIN_MAX_FAILS):
            r = await client.post(
                "/g/pin-lockout-gallery/pin",
                data={"pin": "000000"},
                headers={"x-eos-client-ip": f"203.0.113.{i + 1}"},
            )
            assert r.status_code == 401
        # The per-gallery counter trips anyway, for a brand-new IP ...
        locked = await client.post(
            "/g/pin-lockout-gallery/pin",
            data={"pin": "000000"},
            headers={"x-eos-client-ip": "198.51.100.7"},
        )
        assert locked.status_code == 429
        # ... and even the correct PIN is refused while the gallery is locked.
        correct = await client.post(
            "/g/pin-lockout-gallery/pin",
            data={"pin": "654321"},
            headers={"x-eos-client-ip": "198.51.100.8"},
        )
        assert correct.status_code == 429


@pytest.mark.asyncio
async def test_legacy_password_login_refused_in_productionish_mode(app_env_http, monkeypatch):
    monkeypatch.setattr(security.config, "COOKIE_SECURE", True)
    transport = ASGITransport(app=app_env_http)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        refused = await client.post(
            "/admin/login",
            data={"password": "test-admin-pass"},
            follow_redirects=False,
        )
    assert refused.status_code == 401
