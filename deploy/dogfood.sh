#!/usr/bin/env bash
# Seed one realistic listing (1420 Maple Dr) for local dogfooding.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

# Load .env when present (bootstrap email/password).
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export EOS_BOOTSTRAP_EMAIL="${EOS_BOOTSTRAP_EMAIL:-owner@localhost}"
export EOS_ADMIN_PASSWORD="${EOS_ADMIN_PASSWORD:-dogfood-admin}"

.venv/bin/python -m eos.dogfood "$@"