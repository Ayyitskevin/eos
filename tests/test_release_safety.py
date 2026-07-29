"""Regression tests for release-critical environment defaults and validation."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STRIPE_CHECKER = ROOT / "scripts" / "check-stripe-env.py"
ENV_CHECKER = ROOT / "scripts" / "check-env.py"
ENV_EXAMPLE = ROOT / ".env.example"
SERVICE_UNITS = (ROOT / "deploy" / "eos.service", ROOT / "deploy" / "eos-user.service")
BACKUP_SCRIPT = ROOT / "deploy" / "backup.sh"
VERIFY_BACKUP_SCRIPT = ROOT / "deploy" / "verify-backup.sh"
CRON_BACKUP_SCRIPT = ROOT / "deploy" / "cron-backup.sh"
CRON_VERIFY_BACKUP_SCRIPT = ROOT / "deploy" / "cron-verify-backup.sh"
BACKUP_TIMER_UNITS = (
    ROOT / "deploy" / "eos-backup.service",
    ROOT / "deploy" / "eos-backup.timer",
    ROOT / "deploy" / "eos-verify-backup.service",
    ROOT / "deploy" / "eos-verify-backup.timer",
)
LOGROTATE_CONFIG = ROOT / "deploy" / "eos.logrotate"
CADDYFILE = ROOT / "deploy" / "Caddyfile"
NGINX_CONFIG = ROOT / "deploy" / "nginx-eos.conf"
INSTALL_SCRIPT = ROOT / "deploy" / "install.sh"
INSTALL_USER_SCRIPT = ROOT / "deploy" / "install-user.sh"
READY_MESSAGE = "Stripe test env ready"


def _seed_backup_data(data_dir: Path) -> None:
    (data_dir / "media" / "1").mkdir(parents=True)
    (data_dir / "media" / "1" / "image.jpg").write_bytes(b"synthetic-image")
    with sqlite3.connect(data_dir / "eos.db") as con:
        con.execute("CREATE TABLE proof (value TEXT NOT NULL)")
        con.execute("INSERT INTO proof VALUES ('recoverable')")


def _backup_env(data_dir: Path, backup_dir: Path, **overrides: str) -> dict[str, str]:
    env = {
        "EOS_DATA_DIR": str(data_dir),
        "EOS_BACKUP_DIR": str(backup_dir),
        # never source a developer's real .env during tests
        "EOS_ENV_FILE": "/nonexistent/eos-test.env",
        "PATH": os.environ.get("PATH", ""),
        "LC_ALL": "C",
    }
    env.update(overrides)
    return env


class _PingRecorder:
    def __init__(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        paths: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                paths.append(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.paths = paths
        import threading

        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/hc/eos"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _stripe_env(**overrides: str) -> dict[str, str]:
    env = {
        "EOS_STRIPE_PLATFORM_SECRET_KEY": "sk_test_dummy_platform",
        "EOS_STRIPE_SECRET_KEY": "",
        "EOS_STRIPE_PLATFORM_WEBHOOK_SECRET": "whsec_dummy_platform",
        "EOS_STRIPE_PRICE_STARTER": "price_dummy_starter",
        "EOS_STRIPE_PRICE_PRO": "price_dummy_pro",
    }
    env.update(overrides)
    return env


def _run_stripe_checker(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(STRIPE_CHECKER)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def _production_env(**overrides: str) -> dict[str, str]:
    env = {
        "EOS_CHECK_MODE": "production",
        "EOS_SECRET_KEY": "production-secret-key-32-characters-minimum",
        "EOS_ADMIN_PASSWORD": "production-admin-password",
        "EOS_BASE_URL": "https://eos.example.test",
        "EOS_COOKIE_SECURE": "true",
        "EOS_SAAS_MODE": "true",
        "EOS_SIGNUP_ENABLED": "true",
        "EOS_SIGNUP_AUTO_VERIFY_LOCAL": "false",
        "EOS_BASE_DOMAIN": "eos.example.test",
        "EOS_PLATFORM_ADMIN_EMAILS": "admin@example.test",
        "EOS_STRIPE_PLATFORM_SECRET_KEY": "sk_live_production-placeholder",
        "EOS_STRIPE_PLATFORM_WEBHOOK_SECRET": "whsec_production-placeholder",
        "EOS_STRIPE_PRICE_STARTER": "price_starter_placeholder",
        "EOS_STRIPE_PRICE_PRO": "price_pro_placeholder",
        "EOS_EMAIL_PROVIDER": "postmark",
        "EOS_POSTMARK_API_KEY": "pm-production-test-placeholder",
        "EOS_POSTMARK_FROM_EMAIL": "notify@example.test",
    }
    env.update(overrides)
    return env


def _run_env_checker(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ENV_CHECKER)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


@pytest.mark.parametrize(
    ("key", "live_key"),
    [
        ("EOS_STRIPE_PLATFORM_SECRET_KEY", "sk_live_dummy_platform"),
        ("EOS_STRIPE_SECRET_KEY", "sk_live_dummy_legacy"),
    ],
    ids=("platform-live-key", "legacy-live-key"),
)
def test_stripe_checker_rejects_live_keys_without_claiming_readiness(
    key: str, live_key: str
) -> None:
    result = _run_stripe_checker(_stripe_env(**{key: live_key}))
    output = result.stdout + result.stderr

    assert result.returncode != 0, output
    assert f"ERROR {key}" in result.stdout
    assert READY_MESSAGE not in output


def test_stripe_checker_accepts_complete_test_connect_configuration() -> None:
    result = _run_stripe_checker(_stripe_env())

    assert result.returncode == 0, result.stdout + result.stderr
    assert READY_MESSAGE in result.stdout
    assert "EOS_STRIPE_SECRET_KEY is blank" in result.stdout
    assert result.stderr == ""


def test_production_signup_env_requires_transactional_email() -> None:
    result = _run_env_checker(_production_env(EOS_POSTMARK_API_KEY=""))

    assert result.returncode != 0
    assert "hosted signup requires EOS_POSTMARK_API_KEY" in result.stderr
    assert "env check ok" not in result.stdout


def test_production_signup_env_rejects_local_auto_verify() -> None:
    result = _run_env_checker(_production_env(EOS_SIGNUP_AUTO_VERIFY_LOCAL="true"))

    assert result.returncode != 0
    assert "EOS_SIGNUP_AUTO_VERIFY_LOCAL must be false" in result.stderr
    assert "env check ok" not in result.stdout


def test_production_signup_env_accepts_configured_postmark() -> None:
    result = _run_env_checker(_production_env())

    assert result.returncode == 0, result.stdout + result.stderr
    assert "env check ok" in result.stdout


def test_production_env_rejects_checked_in_admin_password_placeholder() -> None:
    result = _run_env_checker(_production_env(EOS_ADMIN_PASSWORD="change-me-strong-password"))

    assert result.returncode != 0
    assert "EOS_ADMIN_PASSWORD is missing or default" in result.stderr
    assert "env check ok" not in result.stdout


@pytest.mark.parametrize(
    "missing_key",
    [
        "EOS_STRIPE_PLATFORM_SECRET_KEY",
        "EOS_STRIPE_PLATFORM_WEBHOOK_SECRET",
        "EOS_STRIPE_PRICE_STARTER",
        "EOS_STRIPE_PRICE_PRO",
    ],
)
def test_production_saas_env_requires_complete_platform_billing(missing_key: str) -> None:
    result = _run_env_checker(_production_env(**{missing_key: ""}))

    assert result.returncode != 0
    assert "hosted SaaS billing requires" in result.stderr
    assert missing_key in result.stderr
    assert "env check ok" not in result.stdout


def test_env_example_is_sourceable_and_defaults_to_loopback_solo_mode() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    command = """
set -euo pipefail
source "$1"
printf '%s\\n' \
  "$EOS_HOST" \
  "$EOS_BASE_URL" \
  "$EOS_SAAS_MODE" \
  "$EOS_BASE_DOMAIN" \
  "$EOS_SITE_NAME" \
  "$EOS_DEMO_ENABLED"
"""

    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", command, "bash", str(ENV_EXAMPLE)],
        cwd=ROOT,
        env={"LC_ALL": "C", "PATH": ""},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.splitlines() == [
        "127.0.0.1",
        "http://127.0.0.1:8410",
        "false",
        "",
        "Eos Photography",
        "false",
    ]


@pytest.mark.parametrize("service_path", SERVICE_UNITS, ids=lambda path: path.name)
def test_supported_service_units_use_single_application_worker(service_path: Path) -> None:
    unit = service_path.read_text()

    exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert "--host 127.0.0.1" in exec_start
    assert "--host 0.0.0.0" not in exec_start
    assert "--workers 1" in exec_start
    assert "--workers 2" not in unit


def test_proxy_templates_require_apex_and_wildcard_tls() -> None:
    caddy = CADDYFILE.read_text()
    nginx = NGINX_CONFIG.read_text()

    assert "eos.example.com, *.eos.example.com" in caddy
    assert "tls /etc/eos/tls/fullchain.pem /etc/eos/tls/privkey.pem" in caddy
    assert "header_up X-Eos-Client-IP {client_ip}" in caddy
    assert "server_name eos.example.com *.eos.example.com;" in nginx
    assert "listen 443 ssl" in nginx
    assert "ssl_certificate " in nginx
    assert "proxy_set_header X-Eos-Client-IP $remote_addr;" in nginx
    assert "listen 80 default_server;" in nginx
    assert "listen [::]:80 default_server;" in nginx
    assert "server_name _;" in nginx
    unknown_host_sink = nginx.index("listen 80 default_server;")
    valid_host_redirect = nginx.index("return 308 https://$host$request_uri;")
    assert unknown_host_sink < valid_host_redirect
    assert "return 444;" in nginx[unknown_host_sink:valid_host_redirect]


def test_root_installer_preserves_env_and_fails_closed_on_wildcard_setup() -> None:
    script = INSTALL_SCRIPT.read_text()

    assert "--exclude '.env'" in script
    assert 'chmod 600 "$INSTALL_DIR/.env"' in script
    assert 'chmod 700 "$INSTALL_DIR/data" "$INSTALL_DIR/backups"' in script
    assert "Caddy certificate must contain" in script
    assert "caddy validate" in script
    assert "Caddyfile.bak." in script
    assert "|| true" not in script[script.index('if [[ "$INSTALL_CADDY"') :]


def test_backup_and_read_only_restore_drill(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    sqlite_cli = shutil.which("sqlite3")
    assert bash is not None
    if sqlite_cli is None:
        pytest.skip("sqlite3 CLI not installed on this host")

    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    (data_dir / "media" / "1").mkdir(parents=True)
    (data_dir / "brand").mkdir()
    (data_dir / "media" / "1" / "image.jpg").write_bytes(b"synthetic-image")
    (data_dir / "brand" / "logo.txt").write_text("synthetic-brand")
    with sqlite3.connect(data_dir / "eos.db") as con:
        con.execute("CREATE TABLE proof (value TEXT NOT NULL)")
        con.execute("INSERT INTO proof VALUES ('recoverable')")

    env = {
        "EOS_DATA_DIR": str(data_dir),
        "PATH": os.environ.get("PATH", ""),
        "LC_ALL": "C",
    }
    created = subprocess.run(
        [bash, str(BACKUP_SCRIPT), str(backup_dir)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert created.returncode == 0, created.stdout + created.stderr

    db_backup = next(backup_dir.glob("eos-*.db"))
    media_backup = next(backup_dir.glob("eos-media-*.tar.gz"))
    manifest = next(backup_dir.glob("eos-*.sha256"))
    assert backup_dir.stat().st_mode & 0o777 == 0o700
    assert db_backup.stat().st_mode & 0o777 == 0o600
    assert media_backup.stat().st_mode & 0o777 == 0o600
    assert manifest.stat().st_mode & 0o777 == 0o600
    verified = subprocess.run(
        [bash, str(VERIFY_BACKUP_SCRIPT), str(db_backup), str(media_backup), str(manifest)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert "restore drill passed" in verified.stdout


def test_cron_backup_local_only_warns_and_prunes(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    if bash is None or shutil.which("sqlite3") is None or shutil.which("curl") is None:
        pytest.skip("bash/sqlite3/curl CLI required")

    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _seed_backup_data(data_dir)
    stale = backup_dir / "eos-20000101-000000-1.db"
    stale.write_bytes(b"stale")
    old = 1_577_836_800  # 2020-01-01
    os.utime(stale, (old, old))

    env = _backup_env(data_dir, backup_dir)
    result = subprocess.run(
        [bash, str(CRON_BACKUP_SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "local-only" in result.stderr
    assert next(backup_dir.glob("eos-*.db")).is_file()
    assert not stale.exists()


def test_cron_backup_and_verify_ping_healthcheck(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    if bash is None or shutil.which("sqlite3") is None or shutil.which("curl") is None:
        pytest.skip("bash/sqlite3/curl CLI required")

    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    _seed_backup_data(data_dir)
    recorder = _PingRecorder()
    try:
        env = _backup_env(data_dir, backup_dir, EOS_HEALTHCHECK_URL=recorder.url)
        backup = subprocess.run(
            [bash, str(CRON_BACKUP_SCRIPT)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert backup.returncode == 0, backup.stdout + backup.stderr

        verify = subprocess.run(
            [bash, str(CRON_VERIFY_BACKUP_SCRIPT)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert verify.returncode == 0, verify.stdout + verify.stderr
        assert "restore drill passed" in verify.stdout

        empty_env = _backup_env(tmp_path / "empty", tmp_path / "empty-backups")
        empty_env["EOS_HEALTHCHECK_URL"] = recorder.url
        failed = subprocess.run(
            [bash, str(CRON_BACKUP_SCRIPT)],
            cwd=ROOT,
            env=empty_env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert failed.returncode != 0
    finally:
        recorder.close()

    assert recorder.paths == ["/hc/eos", "/hc/eos", "/hc/eos/fail"]


def test_installers_wire_backup_schedules_and_release_rollback() -> None:
    script = INSTALL_SCRIPT.read_text()
    user_script = INSTALL_USER_SCRIPT.read_text()
    logrotate = LOGROTATE_CONFIG.read_text()

    assert "/etc/cron.d/eos-backup" in script
    assert "cron-backup.sh" in script
    assert "cron-verify-backup.sh" in script
    assert "eos.logrotate" in script
    assert "/var/log/eos-backup.log" in logrotate
    for text in (script, user_script):
        assert "releases/" in text
        assert 'ln -sfn "$RELEASE_DIR" "$INSTALL_DIR/current"' in text
        assert "forward-only" in text
    assert "eos-backup.timer" in user_script
    assert "eos-verify-backup.timer" in user_script
    for unit in BACKUP_TIMER_UNITS:
        assert unit.is_file(), unit
    timer_text = "".join(unit.read_text() for unit in BACKUP_TIMER_UNITS)
    assert "WantedBy=timers.target" in timer_text
    assert "Persistent=true" in timer_text
    for unit in SERVICE_UNITS:
        assert "/current/" in unit.read_text()


def test_healthcheck_ping_uses_fail_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    import eos.monitoring as monitoring

    recorder = _PingRecorder()
    try:
        monkeypatch.setenv("EOS_HEALTHCHECK_URL", recorder.url)
        monitoring.ping_healthcheck(ok=True, note="ok")
        monitoring.ping_healthcheck(ok=False, note="jobs_failed=2")
    finally:
        recorder.close()

    assert recorder.paths == ["/hc/eos", "/hc/eos/fail"]

    monkeypatch.delenv("EOS_HEALTHCHECK_URL")
    monitoring.ping_healthcheck(ok=False)  # no-op without a URL configured


def test_job_retry_backoff_is_bounded_and_jittered() -> None:
    import eos.jobs as jobs

    first = jobs._retry_delay(1)
    second = jobs._retry_delay(2)
    assert jobs.RETRY_BASE_SECONDS <= first <= 2 * jobs.RETRY_BASE_SECONDS
    assert 2 * jobs.RETRY_BASE_SECONDS <= second <= 3 * jobs.RETRY_BASE_SECONDS
    assert jobs._retry_delay(50) <= jobs.RETRY_MAX_SECONDS + jobs.RETRY_BASE_SECONDS


def test_job_pool_stop_drains_in_flight_work() -> None:
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    import eos.jobs as jobs

    done = threading.Event()

    def slow() -> None:
        time.sleep(0.3)
        done.set()

    jobs._pool = ThreadPoolExecutor(max_workers=1)
    jobs._pool.submit(slow)
    jobs.stop(timeout=5)

    assert done.is_set()
    assert jobs._pool is None
    jobs._submit(1)  # no-op after drain
    jobs.stop()  # safe with no pool
