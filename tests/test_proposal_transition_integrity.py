"""Atomic public proposal transitions under concurrent requests."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from eos import automations, db, proposals, tenant
from fastapi import HTTPException


@pytest.mark.parametrize("attempt", range(5))
def test_accept_and_decline_race_has_one_winner_and_consistent_effects(
    app_env_http, monkeypatch, attempt
):
    tenant.set_studio("default")
    listing_id = db.run(
        "INSERT INTO listings (studio_id, title, status) VALUES ('default', ?, 'lead')",
        (f"Proposal race {attempt}",),
    )
    slug = f"proposal-race-{attempt}"
    db.run(
        """INSERT INTO proposals (studio_id, listing_id, slug, title, status)
           VALUES ('default', ?, ?, 'Photography services', 'sent')""",
        (listing_id, slug),
    )

    booked_calls: list[int] = []
    calls_lock = threading.Lock()
    start = threading.Barrier(2)

    def record_booking(called_listing_id: int) -> None:
        with calls_lock:
            booked_calls.append(called_listing_id)

    monkeypatch.setattr(automations, "on_listing_booked", record_booking)

    def transition(action: str) -> tuple[str, int]:
        tenant.set_studio("default")
        start.wait(timeout=5)
        try:
            if action == "accept":
                proposals.accept_by_slug(slug)
            else:
                proposals.decline_by_slug(slug)
        except HTTPException as exc:
            return action, exc.status_code
        return action, 200

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(transition, ("accept", "decline")))

    assert sorted(status for _, status in outcomes) == [200, 409]
    winner = next(action for action, status in outcomes if status == 200)
    proposal = db.one("SELECT status FROM proposals WHERE slug=? AND studio_id='default'", (slug,))
    listing = db.one(
        "SELECT status FROM listings WHERE id=? AND studio_id='default'", (listing_id,)
    )

    if winner == "accept":
        assert proposal["status"] == "accepted"
        assert listing["status"] == "booked"
        assert booked_calls == [listing_id]
    else:
        assert proposal["status"] == "declined"
        assert listing["status"] == "lead"
        assert booked_calls == []
