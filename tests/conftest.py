"""Shared pytest fixtures for Eos."""

from __future__ import annotations

import importlib
from collections.abc import Iterable

import eos.config as config
import eos.db as db
import eos.jobs as jobs
import eos.main as main
import eos.security as security
import pytest


def reload_modules(modules: Iterable) -> None:
    for mod in modules:
        importlib.reload(mod)


def _solo_test_env(monkeypatch) -> None:
    """Reset SaaS flags so a developer's .env does not break solo-mode tests."""
    monkeypatch.setenv("EOS_SAAS_MODE", "false")
    monkeypatch.setenv("EOS_SIGNUP_ENABLED", "false")
    monkeypatch.setenv("EOS_SIGNUP_INVITE_ONLY", "false")
    monkeypatch.setenv("EOS_BILLING_ENFORCE", "false")


@pytest.fixture(autouse=True)
def _isolate_dev_env(monkeypatch):
    _solo_test_env(monkeypatch)


def _set_base_env(monkeypatch, tmp_path, **extra: str) -> None:
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_DEMO_ENABLED", "false")
    _solo_test_env(monkeypatch)
    for key, val in extra.items():
        monkeypatch.setenv(key, val)


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """FastAPI app with isolated DB and background job workers."""
    _set_base_env(monkeypatch, tmp_path, EOS_BASE_URL="http://testserver")
    reload_modules((config, db, jobs, main))
    config.ensure_dirs()
    db.migrate()
    jobs.start()
    yield main.app
    jobs.stop()


@pytest.fixture()
def app_env_http(tmp_path, monkeypatch):
    """App fixture without job workers — for lightweight HTTP smoke tests."""
    _set_base_env(monkeypatch, tmp_path, EOS_BASE_URL="http://testserver")
    reload_modules((config, db, security, main))
    config.ensure_dirs()
    db.migrate()
    return main.app
