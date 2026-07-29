"""Scheduler integrity: tick drains, failure isolation, bucketed sweep keys, restart.

Tests stub the drain callables and the lazily imported integration modules so only
eos/scheduler.py's public behavior (start/stop/_loop cadence) is exercised.
"""

from __future__ import annotations

import importlib
import time
from types import SimpleNamespace
from unittest.mock import Mock

import eos.config as config
import eos.jobs as jobs
import eos.monitoring as monitoring
import eos.scheduler as scheduler
import eos.sms as sms
import pytest


@pytest.fixture()
def scheduler_env(monkeypatch):
    """Fast ticks with all drains and integration effects stubbed."""
    monkeypatch.setattr(config, "SEQUENCE_TICK_SECONDS", 0.02)
    monkeypatch.setattr(config, "INTEGRATION_TICK_SECONDS", 0.02)
    drains = SimpleNamespace(
        sequences=Mock(return_value=0),
        webhooks=Mock(return_value=0),
        delivery_notify=Mock(return_value=0),
        commerce=Mock(return_value=0),
    )
    monkeypatch.setattr(scheduler.sequences, "process_due", drains.sequences)
    monkeypatch.setattr(scheduler.webhooks, "process_pending", drains.webhooks)
    monkeypatch.setattr(scheduler.delivery_notify, "process_pending", drains.delivery_notify)
    monkeypatch.setattr(scheduler.commerce, "expire_all_pending_bookings", drains.commerce)
    effects = SimpleNamespace(
        enqueue=Mock(),
        reminders=Mock(),
        health=Mock(),
    )
    monkeypatch.setattr(jobs, "enqueue", effects.enqueue)
    monkeypatch.setattr(sms, "shoot_day_reminders", effects.reminders)
    monkeypatch.setattr(monitoring, "report_health", effects.health)
    yield drains, effects
    scheduler.stop()


def _wait_for(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition not met within timeout")


def test_every_drain_fires_each_tick(scheduler_env):
    drains, _effects = scheduler_env
    scheduler.start()

    _wait_for(
        lambda: (
            min(
                drains.sequences.call_count,
                drains.webhooks.call_count,
                drains.delivery_notify.call_count,
                drains.commerce.call_count,
            )
            >= 2
        )
    )
    scheduler.stop()

    for drain in (
        drains.sequences,
        drains.webhooks,
        drains.delivery_notify,
        drains.commerce,
    ):
        assert drain.call_count >= 2


def test_failing_drain_does_not_block_other_drains(scheduler_env):
    drains, _effects = scheduler_env
    drains.sequences.side_effect = RuntimeError("sequence store offline")
    scheduler.start()

    _wait_for(
        lambda: (
            drains.webhooks.call_count >= 2
            and drains.delivery_notify.call_count >= 2
            and drains.commerce.call_count >= 2
        )
    )
    scheduler.stop()

    assert drains.sequences.call_count >= 2


def test_integration_sweep_uses_bucketed_idempotency_key(scheduler_env, monkeypatch):
    _drains, effects = scheduler_env
    monkeypatch.setattr(scheduler, "time", SimpleNamespace(time=lambda: 12345.0))
    scheduler.start()

    _wait_for(lambda: effects.enqueue.call_count >= 1)
    scheduler.stop()

    args, kwargs = effects.enqueue.call_args
    assert args == ("integration_sweep", {})
    assert kwargs == {"idempotency_key": "integration_sweep:12345"}
    assert effects.reminders.call_count >= 1
    assert effects.health.call_count >= 1


def test_integration_sweep_key_is_stable_within_bucket_and_rolls_over(scheduler_env, monkeypatch):
    _drains, effects = scheduler_env
    clock = {"now": 2000.0}
    monkeypatch.setattr(scheduler, "time", SimpleNamespace(time=lambda: clock["now"]))
    scheduler.start()

    _wait_for(lambda: effects.enqueue.call_count >= 2)
    keys_within_bucket = [call.kwargs["idempotency_key"] for call in effects.enqueue.mock_calls]

    clock["now"] = 2001.5  # next whole-second bucket
    _wait_for(
        lambda: any(
            call.kwargs["idempotency_key"] != keys_within_bucket[0]
            for call in effects.enqueue.mock_calls
        )
    )
    scheduler.stop()

    assert set(keys_within_bucket) == {"integration_sweep:2000"}
    all_keys = [call.kwargs["idempotency_key"] for call in effects.enqueue.mock_calls]
    assert "integration_sweep:2001" in all_keys
    assert all(key.startswith("integration_sweep:") for key in all_keys)


def test_restart_after_stop_resumes_ticks(scheduler_env):
    drains, _effects = scheduler_env
    scheduler.start()
    _wait_for(lambda: drains.webhooks.call_count >= 1)
    scheduler.stop()
    assert scheduler._thread is None
    first_run_calls = drains.webhooks.call_count

    scheduler.start()
    _wait_for(lambda: drains.webhooks.call_count > first_run_calls)
    scheduler.stop()

    assert scheduler._thread is None
    assert drains.webhooks.call_count > first_run_calls


def test_module_reload_simulated_restart_keeps_single_thread(scheduler_env):
    drains, _effects = scheduler_env
    scheduler.start()
    _wait_for(lambda: drains.webhooks.call_count >= 1)
    scheduler.stop()

    importlib.reload(scheduler)
    try:
        scheduler.start()
        _wait_for(lambda: drains.webhooks.call_count >= 2)
        scheduler.stop()
    finally:
        scheduler.stop()

    assert scheduler._thread is None
    assert drains.webhooks.call_count >= 2
