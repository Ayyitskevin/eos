# Eos

**Multi-tenant SaaS** for real estate photography studios — booking, listing pipeline, branded delivery, agent portals, and Stripe Connect payments. Competes with Aryeo-class platforms; also runs as a single-studio install.

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

### Phase 6 (shipped)

- Self-serve booking at `/book` — package picker, add-ons, live availability slots
- Typed e-sign at checkout; auto-creates client, listing, appointment, proposal
- Deposit invoices via Stripe Checkout; slot held until deposit paid
- Booking confirmation page at `/booking/{token}`
- Studio admin — package editor, booking hours/buffers, order inbox

### Phase 7 (shipped)

- Agent portal at `/portal/{token}` — delivery history, gallery links, pay invoice
- Pay-to-download — lock ZIP/originals until listing invoice is paid
- Watermarked gallery previews until paid
- Rich media embeds per listing (Matterport, YouTube, Vimeo) on gallery page
- Auto-email gallery link on publish (optional, Studio settings)
- Portal link on client detail page

### Phase 8 (shipped)

- Property microsite per listing at `/l/{slug}` — hero, specs, agent branding, tour embeds
- MLS download center — one-click MLS, Zillow, and full-res ZIP bundles
- Marketing kit job queue — IG square/story graphics and printable flyer PDF
- Social share meta (OG tags) for agent link sharing
- Optional lead-capture form on property site
- Auto-publish property site when gallery goes live (Studio settings)

### Phase 9 (shipped)

- Ops reports at `/admin/reports` — revenue MTD/YTD, AOV, overdue AR, top agents
- Brokerage billing — invoice parent brokerage with agent attribution; consolidated statements
- Production Kanban at `/admin/kanban` — pipeline board with one-click advance
- Photographer assignment on listings and calendar appointments
- Twilight booking window — evening slots for twilight add-on on `/book`
- Delivery upsell prompts on gallery, property site, and agent portal

### Phase 10 (shipped) — Eos 1.0

- Multi-tenant studios — subdomain routing (`{slug}.yourdomain.com`), per-studio data isolation
- Studio signup at `/signup` — auto-seeds packages, presets, sequences, owner account
- Sequence editor — customize drip subject/body/delay/trigger in admin
- API tokens + `/api/v1/listings` and `/api/v1/bookings` for Zapier integrations
- Outbound webhooks — `booking.created`, `listing.delivered`, `invoice.paid` with HMAC signatures
- Agent referral codes — booking credit applied at checkout
- Set `EOS_SIGNUP_ENABLED=true` and `EOS_BASE_DOMAIN` for hosted SaaS

### Phase 11 (shipped) — Eos 1.1

- Google Calendar 2-way sync — OAuth connect, push appointments, pull external events, block booking slots
- Dropbox watch-folder ingest — auto-import photos from `{listing_id}/` subfolders into galleries
- Per-studio Stripe platform billing — trial on signup, subscription checkout at `/admin/billing`
- Set `EOS_GOOGLE_*`, `EOS_DROPBOX_*`, and `EOS_STRIPE_PLATFORM_*` env vars for hosted SaaS

### Phase 14 (shipped) — Eos 1.4 scale & retention

- Agent self-reschedule from portal — pick open slot, confirm without calling
- Team calendar — photographer color coding + filter (extends Phase 13 calendar)
- Agent credit balances — ledger, apply at booking checkout
- Dropbox scan-now + retry failed ingests in Studio settings
- Signup email verification + onboarding wizard for new SaaS studios
- CSV report export at `/admin/reports/export.csv`
- Granular RBAC — scheduler, editor, accountant roles
- Twilio SMS shoot-day reminders + SMS sequence channel
- Brokerage self-serve portal at `/portal/brokerage/{token}`
- Churn alerts — inactive agents (90+ days) on dashboard
- Studio-namespaced media storage (`media/{studio_id}/{gallery_id}/`)

### Phase 13 (shipped) — Eos 1.3 operations & monetization

- Visual shoot calendar — month/week views, drag-to-reschedule, photographer filter
- Google Calendar busy blocks shown on calendar and in sidebar
- Agent MLS favorites — star photos in gallery; revision rounds on listings
- Plan-tier limits — listings/month, API tokens, webhooks; usage metering on billing page
- Custom domain routing — Pro studios map `custom_domain` to tenant (CNAME + verify in Studio)
- API writes — `POST/PATCH /api/v1/listings`, `POST /api/v1/bookings`, inbound `/api/v1/inbound/{event}`
- Delivery upsell checkout — add-ons from gallery with Stripe Checkout
- IPTC/EXIF metadata on MLS export crops (address, agent, MLS ID)

### Phase 12 (shipped) — Eos 1.2 hardening

- Tenant-bound admin sessions — cross-subdomain access blocked
- `studio_id` guards on uploads, emails, invoices, appointments
- Legacy admin password disabled in SaaS / multi-studio mode
- Billing enforcement (`EOS_BILLING_ENFORCE`) with trial expiry
- CSRF protection (Sec-Fetch-Site), signup rate limiting
- Integration event log + Dropbox ingest history in Studio settings
- Webhook delivery log, httpx timeouts, integration sweep via job queue
- API pagination (`/api/v1/listings`, `/api/v1/bookings`), `/api/v1/me`
- GitHub Actions CI, `deploy/backup.sh`

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