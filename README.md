# Eos

Real estate photography OS for **solo operators** who want **SaaS-quality** delivery — listing-centric pipeline, room-based galleries, MLS export presets, and agent client hierarchy.

Port **8410** (Mise=8400, Hestia=8500).

## Lineage

Eos inherits the best patterns from two sibling projects:

| Source | What Eos keeps |
|--------|----------------|
| **Mise** | FastAPI + Jinja + HTMX, SQLite WAL, gallery PIN delivery, crop presets, brand kits, client hierarchy, jobs/audit spine |
| **Hestia** | Numbered migrations + `schema_migrations`, service packages, appointments, listing tasks checklist |

### What both lacked for RE (Eos adds)

- **`listings`** as first-class entity — address, MLS ID, beds/baths/sqft, `due_at`, turnaround
- **Room-based shot lists** and **room-based gallery sections** (not F&B dish categories)
- **MLS/Zillow crop presets** seeded in DB
- **`client_type`**: agent / brokerage / homeowner / vendor
- Vertical locked to RE — no `shoot_type` enum

### Phase 2 (shipped)

- Photo uploads + Pillow derivatives (web/thumb)
- MLS / Zillow / Instagram export crops with agent brand-kit watermark
- Background job queue (derivatives, exports, ZIP)
- PIN-gated gallery media + download ZIP
- Listing invoices with optional Stripe Checkout
- Shoot calendar (day + twilight appointments)

### Still deferred

Questionnaires, contract e-sign, multi-tenant auth, email delivery templates.

## Quick start

```bash
cd eos
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set EOS_SECRET_KEY and EOS_ADMIN_PASSWORD
uvicorn eos.main:app --host 127.0.0.1 --port 8410
```

- Admin: http://localhost:8410/admin
- Public site: http://localhost:8410/
- Health: http://localhost:8410/healthz

## Tests

```bash
pytest tests/ -v
```

## Schema

Single migration `0001_baseline.sql` — studio, clients, listings, listing_shots, listing_tasks, galleries, sections, assets, crop_presets, brand_kits, service_packages, proposals, invoices, appointments, audit_log, jobs.

All tables carry `studio_id='default'` for a future SaaS tier; v0 runs as a single admin password (Mise pattern).