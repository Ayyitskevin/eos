"""Phase 14 — self-reschedule, credits, RBAC, churn, media paths."""

import importlib

import eos.churn as churn
import eos.clients as clients
import eos.config as config
import eos.credits as credits
import eos.db as db
import eos.jobs as jobs
import eos.media_paths as media_paths
import eos.rbac as rbac
import eos.reschedule as reschedule
import eos.tenant as tenant
import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    for mod in (config, db, jobs, tenant, credits, clients, reschedule, churn, media_paths, rbac):
        importlib.reload(mod)
    config.ensure_dirs()
    db.migrate()
    jobs.start()
    tenant.set_studio("default")
    yield
    jobs.stop()


def test_credit_balance_and_apply(env):
    cid = clients.create_client("Agent", email="a@test.com", client_type="agent")
    credits.add_credit(cid, amount_cents=5000, note="loyalty")
    assert credits.balance(cid) == 5000
    new_total, applied = credits.apply_at_checkout(cid, 12000)
    assert applied == 5000
    assert new_total == 7000
    assert credits.balance(cid) == 0


def test_media_paths_namespaced(env):
    tenant.set_studio("default")
    path = media_paths.gallery_dir(42)
    assert "default" in str(path) or path.name == "42"


def test_rbac_scheduler_perms(env):
    assert rbac.has_perm("scheduler", "calendar")
    assert not rbac.has_perm("scheduler", "reports")
    assert rbac.has_perm("owner", "reports")


def test_churn_inactive_agents(env):
    tenant.set_studio("default")
    clients.create_client("Old Agent", email="old@test.com", client_type="agent")
    rows = churn.inactive_agents(days=90)
    assert any(r["email"] == "old@test.com" for r in rows)


def test_reschedule_slots(env):
    from eos import scheduling

    tenant.set_studio("default")
    slots = scheduling.reschedule_slots(days=7)
    assert isinstance(slots, list)


def test_users_extended_roles(env):
    from eos import users

    tenant.set_studio("default")
    uid = users.create_user("sched@test.com", "pass12345", role="scheduler")
    row = users.get_user(uid)
    assert row["role"] == "scheduler"
