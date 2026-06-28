#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/uvicorn ]]; then
  make install
fi

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export EOS_SECRET_KEY="${EOS_SECRET_KEY:-dev-secret-key-32chars-minimum!!}"
export EOS_ADMIN_PASSWORD="${EOS_ADMIN_PASSWORD:-dev}"

exec .venv/bin/uvicorn eos.main:app --host "${EOS_HOST:-127.0.0.1}" --port "${EOS_PORT:-8410}" --reload