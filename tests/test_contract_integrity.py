"""Tenant-branded contract snapshots and atomic public signing."""

from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from eos import contracts, db, tenant
from fastapi import HTTPException


def test_contract_snapshots_tenant_studio_identity(app_env_http):
    db.run("INSERT INTO studio (id, name, slug) VALUES ('alpha', 'Alpha Imaging', 'alpha')")
    tenant.set_studio("alpha")
    listing_id = db.run("INSERT INTO listings (studio_id, title) VALUES ('alpha', '12 Tenant Way')")

    contract_id = contracts.create_contract(listing_id)
    body = db.one("SELECT body FROM contracts WHERE id=?", (contract_id,))["body"]
    db.run("UPDATE studio SET name='Renamed Later' WHERE id='alpha'")

    assert 'Alpha Imaging ("Photographer")' in body
    assert "Renamed Later" not in body
    assert db.one("SELECT body FROM contracts WHERE id=?", (contract_id,))["body"] == body


@pytest.mark.parametrize("attempt", range(5))
def test_first_contract_signer_wins_atomically(app_env_http, attempt):
    tenant.set_studio("default")
    listing_id = db.run(
        "INSERT INTO listings (studio_id, title) VALUES ('default', ?)",
        (f"Contract race {attempt}",),
    )
    contract_id = contracts.create_contract(listing_id)
    contracts.mark_sent(contract_id)
    slug = db.one("SELECT slug FROM contracts WHERE id=?", (contract_id,))["slug"]
    start = threading.Barrier(2)

    def sign(signer: str) -> tuple[str, int]:
        tenant.set_studio("default")
        start.wait(timeout=5)
        try:
            contracts.sign_by_slug(slug, signer, f"192.0.2.{1 if signer == 'Alice' else 2}")
        except HTTPException as exc:
            return signer, exc.status_code
        return signer, 200

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(sign, ("Alice", "Bob")))

    assert sorted(status for _, status in outcomes) == [200, 409]
    winner = next(signer for signer, status in outcomes if status == 200)
    row = db.one(
        "SELECT status, signer_name, signer_ip, signed_at FROM contracts WHERE id=?",
        (contract_id,),
    )
    assert row["status"] == "signed"
    assert row["signer_name"] == winner
    assert row["signer_ip"] == f"192.0.2.{1 if winner == 'Alice' else 2}"
    assert row["signed_at"]


@pytest.mark.parametrize("attempt", range(5))
def test_contract_edit_and_send_race_preserves_exact_signed_snapshot(app_env_http, attempt):
    tenant.set_studio("default")
    listing_id = db.run(
        "INSERT INTO listings (studio_id, title) VALUES ('default', ?)",
        (f"Contract edit race {attempt}",),
    )
    contract_id = contracts.create_contract(listing_id)
    original = db.one("SELECT body FROM contracts WHERE id=?", (contract_id,))["body"]
    edited = f"{original}\nOwner-approved amendment {attempt}."
    start = threading.Barrier(2)

    def transition(action: str) -> tuple[str, int]:
        tenant.set_studio("default")
        start.wait(timeout=5)
        try:
            if action == "edit":
                contracts.update_contract(contract_id, title="Updated agreement", body=edited)
            else:
                contracts.mark_sent(contract_id)
        except HTTPException as exc:
            return action, exc.status_code
        return action, 200

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(transition, ("edit", "send")))

    assert next(status for action, status in outcomes if action == "send") == 200
    assert next(status for action, status in outcomes if action == "edit") in {200, 400, 409}
    row = db.one(
        "SELECT status, body, body_sha256 FROM contracts WHERE id=?",
        (contract_id,),
    )
    assert row["status"] == "sent"
    assert row["body"] in {original, edited}
    assert row["body_sha256"] == hashlib.sha256(row["body"].encode()).hexdigest()
