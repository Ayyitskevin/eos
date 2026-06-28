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

### Phase 3 (shipped)

- RE proposals from service packages — client accept/decline at `/p/{slug}`
- Contracts with typed e-sign at `/c/{slug}`
- Manual email delivery (Gmail SMTP) for galleries, proposals, contracts, invoices
- Gallery section reorder, rename, asset move/reorder within sections

### Phase 4 (shipped)

- RE pre-shoot questionnaire at `/q/{token}` — merges into listing access notes
- Public booking form at `/book` with inquiry inbox in Studio settings
- Today view — shoots, deliveries due, pending intake
- Studio settings — publish marketing site, headline, service area
- Activity log + sent emails history
- Listing automations (questionnaire → shooting, publish gallery → delivered)
- Gallery cover image for client delivery header

### Phase 5 (shipped)

- Operator accounts — email + scrypt password, owner/operator roles
- Dual login — legacy solo password or user email (auto-switches when accounts exist)
- Email drip sequences on `listing.booked`, `listing.delivered`, `proposal.sent`
- Sequences admin — toggle sequences, view/cancel pending runs
- In-process scheduler drains due sequence emails via Gmail SMTP
- Bootstrap owner from `EOS_BOOTSTRAP_EMAIL` + `EOS_ADMIN_PASSWORD`
- Production deploy unit — `deploy/eos.service` for systemd + nginx reverse proxy

### Still deferred

Full multi-tenant billing, per-studio isolation beyond `studio_id`.

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

All tables carry `studio_id='default'` for a future SaaS tier. Solo mode uses a single admin password; set `EOS_BOOTSTRAP_EMAIL` to create an owner account and enable email login.

## Production deploy

```bash
# On server (example: /opt/eos)
sudo useradd -r -m -d /opt/eos eos
sudo cp -r . /opt/eos && sudo chown -R eos:eos /opt/eos
cd /opt/eos && python -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo cp deploy/eos.service /etc/systemd/system/
sudo systemctl enable --now eos
```

Put TLS termination on nginx/Caddy proxying to `127.0.0.1:8410`. Set `EOS_COOKIE_SECURE=true` and a strong `EOS_SECRET_KEY` in `/opt/eos/.env`.