# AGENTS.md — Eos

**AI agents: read [docs/AI_AGENTS.md](docs/AI_AGENTS.md) first.** It is the canonical guide — invariants, architecture, how to add features without breaking multi-tenancy.

## TL;DR

- **What:** Multi-tenant RE photography SaaS (Aryeo competitor), v1.7.0, port 8410
- **Stack:** FastAPI + Jinja/HTMX + SQLite + optional S3
- **Rule #1:** Every DB query/mutation must scope `studio_id` via `STUDIO_ID`
- **Rule #2:** Minimal diffs — extend modules, append migrations, run `make test lint`
- **Rule #3:** Push to `main` after shipping; never commit `.env` or `data/`

## Quick paths

| Path | Purpose |
|------|---------|
| `docs/AI_AGENTS.md` | **Full agent reference** |
| `eos/main.py` | Middleware + routers |
| `eos/tenant.py` | Tenant resolution |
| `eos/plan_limits.py` | SaaS plan gating |
| `eos/migrations/` | Append-only SQL |
| `tests/conftest.py` | Test fixtures |

## Commands

```bash
make install && make run && make test && make lint
```

## Docs

- Deploy: [docs/DEPLOY.md](docs/DEPLOY.md)
- Scale: [docs/SCALE.md](docs/SCALE.md)
- Humans: [README.md](README.md)