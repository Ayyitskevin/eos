# AGENTS.md — Eos

## What this is

Eos is a **real estate photography** studio OS: listing pipeline, RE shot lists, room-section galleries, MLS export presets. Solo-operator today; `studio_id` on every table for future SaaS.

Stack: **FastAPI + Jinja2 + HTMX**, SQLite WAL, port **8410**.

## Key paths

| Path | Purpose |
|------|---------|
| `eos/main.py` | App entry, middleware, router mount |
| `eos/vocab.py` | RE vocabulary — rooms, statuses, default shot list |
| `eos/listings.py` | Listing spine — create, shot list, tasks |
| `eos/galleries.py` | Delivery galleries — room sections, PIN |
| `eos/clients.py` | Agent/broker hierarchy |
| `eos/migrations/` | Numbered SQL migrations (Hestia pattern) |
| `templates/admin/` | Pipeline, listings, clients, galleries |
| `static/eos.css` | Dawn/light design system |

## Conventions

- **Listing-centric** — never generic `projects`; everything hangs off `listings`
- **RE vertical locked** — no F&B licensing, press, platekit, album proofing
- **Mise patterns** for security (PIN lockout, signed cookies), slugs, env config
- **Hestia patterns** for migrations ledger, service packages, tasks checklist
- Minimal diffs; match existing naming and module layout

## Running locally

```bash
uvicorn eos.main:app --host 127.0.0.1 --port 8410
pytest tests/ -v
```

Required env: `EOS_SECRET_KEY`, `EOS_ADMIN_PASSWORD`. Data defaults to `./data`.

## Phase boundaries

**Shipped:** through phase 7 — agent portal, pay-to-download, watermarked previews, listing embeds.

**Out of scope:** property microsites (phase 8), full SaaS multi-tenant (phase 10).