"""Idempotency and payload-binding regressions for delivery upsells."""

from __future__ import annotations

import json

import eos.db as db
import eos.tenant as tenant
import eos.upsell as upsell
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient


@pytest.fixture()
def upsell_seed(app_env):
    del app_env
    tenant.set_studio("default")
    client_id = db.run(
        """INSERT INTO clients (studio_id, name, email)
           VALUES ('default', 'Upsell Client', 'upsell@example.test')"""
    )
    other_client_id = db.run(
        """INSERT INTO clients (studio_id, name, email)
           VALUES ('default', 'Other Client', 'other@example.test')"""
    )
    listing_id = db.run(
        """INSERT INTO listings (studio_id, client_id, title, status, address_line1)
           VALUES ('default', ?, 'Upsell Listing', 'delivered', '1 Addon Way')""",
        (client_id,),
    )
    addons = db.all_(
        """SELECT id, price_cents FROM service_addons
           WHERE studio_id='default' AND active=1 ORDER BY id LIMIT 2"""
    )
    assert len(addons) == 2
    return {
        "listing_id": listing_id,
        "client_id": client_id,
        "other_client_id": other_client_id,
        "addon_ids": [int(row["id"]) for row in addons],
        "total_cents": sum(int(row["price_cents"]) for row in addons),
    }


def test_same_key_replays_canonical_payload_without_duplicate_pricing(upsell_seed):
    addon_a, addon_b = upsell_seed["addon_ids"]
    first = upsell.create_order(
        listing_id=upsell_seed["listing_id"],
        addon_ids=[addon_b, addon_a, addon_a],
        request_key="upsell-replay-key",
    )
    replay = upsell.create_order(
        listing_id=upsell_seed["listing_id"],
        addon_ids=[addon_a, addon_b],
        request_key="upsell-replay-key",
    )

    assert replay["order_id"] == first["order_id"]
    assert replay["token"] == first["token"]
    assert replay["invoice"]["id"] == first["invoice"]["id"]
    assert replay["total_cents"] == first["total_cents"] == upsell_seed["total_cents"]
    order = db.one("SELECT * FROM listing_upsell_orders WHERE id=?", (first["order_id"],))
    assert json.loads(order["addon_ids"]) == sorted([addon_a, addon_b])
    assert (
        db.one(
            "SELECT COUNT(*) AS n FROM listing_upsell_orders WHERE listing_id=?",
            (upsell_seed["listing_id"],),
        )["n"]
        == 1
    )
    assert (
        db.one(
            "SELECT COUNT(*) AS n FROM invoices WHERE listing_id=? AND invoice_kind='balance'",
            (upsell_seed["listing_id"],),
        )["n"]
        == 1
    )


@pytest.mark.parametrize("changed_field", ["addons", "client"])
def test_same_key_rejects_changed_payload(upsell_seed, changed_field):
    addon_a, addon_b = upsell_seed["addon_ids"]
    first = upsell.create_order(
        listing_id=upsell_seed["listing_id"],
        addon_ids=[addon_a],
        request_key="upsell-bound-key",
    )
    kwargs = {
        "listing_id": upsell_seed["listing_id"],
        "addon_ids": [addon_a, addon_b] if changed_field == "addons" else [addon_a],
        "request_key": "upsell-bound-key",
        "client_id": (
            upsell_seed["other_client_id"]
            if changed_field == "client"
            else upsell_seed["client_id"]
        ),
    }

    with pytest.raises(HTTPException, match="reused with different input") as exc_info:
        upsell.create_order(**kwargs)

    assert exc_info.value.status_code == 409
    assert (
        db.one(
            "SELECT COUNT(*) AS n FROM listing_upsell_orders WHERE listing_id=?",
            (upsell_seed["listing_id"],),
        )["n"]
        == 1
    )
    assert (
        db.one("SELECT invoice_id FROM listing_upsell_orders WHERE id=?", (first["order_id"],))[
            "invoice_id"
        ]
        == first["invoice"]["id"]
    )


@pytest.mark.parametrize("addon_ids", [[], [999_999]])
def test_invalid_addon_selection_has_no_partial_order(upsell_seed, addon_ids):
    with pytest.raises(HTTPException, match="invalid add-on selection") as exc_info:
        upsell.create_order(
            listing_id=upsell_seed["listing_id"],
            addon_ids=addon_ids,
            request_key="upsell-invalid-key",
        )

    assert exc_info.value.status_code == 400
    assert (
        db.one(
            "SELECT COUNT(*) AS n FROM listing_upsell_orders WHERE listing_id=?",
            (upsell_seed["listing_id"],),
        )["n"]
        == 0
    )
    assert (
        db.one(
            "SELECT COUNT(*) AS n FROM invoices WHERE listing_id=? AND invoice_kind='balance'",
            (upsell_seed["listing_id"],),
        )["n"]
        == 0
    )


@pytest.mark.asyncio
async def test_upsell_capability_page_is_never_cacheable(app_env, upsell_seed):
    order = upsell.create_order(
        listing_id=upsell_seed["listing_id"],
        addon_ids=[upsell_seed["addon_ids"][0]],
        request_key="upsell-private-cache",
    )
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"/upsell/{order['token']}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
