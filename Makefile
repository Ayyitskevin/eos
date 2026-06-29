.PHONY: install dev test smoke lint format run dogfood migrate check-env check-stripe coverage stripe-listen

install:
	python3 -m venv .venv
	.venv/bin/pip install -U pip
	.venv/bin/pip install -r requirements-dev.txt

dev: install

test:
	.venv/bin/pytest tests/ -q

smoke:
	.venv/bin/pytest tests/test_foundation.py tests/test_sql_guardrails.py -q

coverage:
	.venv/bin/pytest tests/ -q --cov=eos --cov-report=term-missing

lint:
	.venv/bin/ruff check eos scripts tests
	.venv/bin/ruff format --check eos scripts tests

format:
	.venv/bin/ruff format eos scripts tests
	.venv/bin/ruff check --fix eos scripts tests

run:
	scripts/dev.sh

dogfood:
	./deploy/dogfood.sh

migrate:
	.venv/bin/python scripts/migrate.py

check-env:
	.venv/bin/python scripts/check-env.py

check-stripe:
	@set -a && [ -f .env ] && . ./.env; set +a; .venv/bin/python scripts/check-stripe-env.py

stripe-listen:
	@echo "Forward platform webhooks to local Eos (paste whsec into EOS_STRIPE_PLATFORM_WEBHOOK_SECRET):"
	stripe listen --forward-to http://127.0.0.1:8410/stripe/platform/webhook

security:
	.venv/bin/bandit -c pyproject.toml -r eos -ll -q
	.venv/bin/pip-audit -r requirements.txt