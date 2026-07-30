# Eos Production Gameplan — competitive gap analysis and build order

Date: 2026-07-30. Basis: full codebase audit (v1.10.0) + competitor research
(Aryeo/Zillow, Spiro, Tonomo, HDPhotoHub, PhotoUp, HomeJab, FloorPlanOnline, Fotello).

## Where Eos stands

Eos already matches or beats competitors on: multi-tenant SaaS + Stripe Connect,
booking with deposits/e-sign, Kanban pipeline, visual calendar + Google sync,
PIN galleries, property sites, agent/brokerage portals, retention tooling
(rebooking cockpit, referral program, acquisition queue, revenue optimizer),
RBAC, API v1 + webhooks, per-tenant email/SMS.

Credible moats to lean on in marketing: self-hosted data ownership (vs Aryeo's
perpetual content license / Zillow data flow), retention tooling depth, no
per-listing platform fees.

## Gaps, ranked by impact-per-effort

### Wave 1 — production blockers and quick wins (build first)

1. ~~**Legal pages** (`/terms`, `/privacy`)~~ — **shipped** (2026-07-30): public platform pages on
   apex + tenant hosts, operator contact from config, linked from marketing footer and signup.
2. ~~**Global + API rate limiting**~~ — **shipped** (2026-07-30): in-process sliding-window
   limiter; per-token `/api/v1` cap and per-IP caps on portal + referral short links
   (`EOS_RATE_LIMIT_*`, 429 + `Retry-After`, health checks exempt). Gallery PIN and booking
   inquiries keep their dedicated throttles.
3. ~~**Gallery-view analytics + scheduled agent email reports**~~ — **shipped** (2026-07-30):
   privacy-preserving view tracking on PIN galleries + property microsites (hashed visitor keys,
   referrer domains only, DNT honored), `/admin/reports/analytics` with 7/30/90-day windows,
   unique visitors, referrers, trends, and CSV export, agent portal view counts, and durable
   weekly per-agent digest emails via the scheduler with a studio opt-out.
4. ~~**Embeddable booking widget**~~ — **shipped** (2026-07-30): chrome-free `/book/embed`
   iframe variant of the existing atomic, replay-keyed booking flow (same POST, same
   activation gates), CSP `frame-ancestors` from a per-studio allowlist (default `*`),
   compact embed confirmation, and a copy-paste iframe snippet in studio settings.
5. ~~**Lead capture on property sites**~~ — **shipped** (2026-07-30): buyer lead form on
   `/l/{slug}` with studio + per-listing toggles, honeypot and per-IP rate limiting,
   tenant-scoped storage with hashed IPs, durable notification intents emailed to the
   listing's agent (or studio contact) via the scheduler, an RBAC-protected leads inbox
   with CSV export and mark-as-contacted, and a lead count on listing admin.

### Wave 2 — competitive differentiation

6. ~~**Auto-rendered slideshow/reel videos**~~ — **shipped** (2026-07-30): one-click
   renders from the gallery admin page — 16:9 1080p slideshows and 9:16 1080x1920
   vertical reels — built by an ffmpeg job in the existing SQLite job queue
   (`gallery_video_render`, replay-safe per gallery+format, durable status in
   `gallery_video_renders`, first `EOS_VIDEO_MAX_PHOTOS` ready photos, H.264 +
   faststart MP4s under the studio-namespaced media path so S3 sync picks them
   up). Delivered on the PIN gallery, agent portal, and property microsite
   behind the same publication/PIN/paywall gates; disabled gracefully when
   ffmpeg is absent or `EOS_VIDEO_RENDER_ENABLED=false`.
7. **AI listing marketing copy** — Tonomo writes brochures/descriptions/IG posts.
   Small LLM-backed feature (optional provider key, fail-closed when unset) next
   to the existing marketing kit. Small-medium.
8. **PWA + mobile front-end pass** — Aryeo Go / HDPhotoHub white-label apps.
   A PWA manifest + responsive admin/portal CSS captures most of the value. Medium.
9. **Email bounce/complaint suppression** — Postmark shipped but zero bounce
   handling; deliverability is a production risk. Small-medium.

### Wave 3 — bigger swings (post-launch candidates)

10. **Smart auto-assignment scheduling** — Tonomo's moat: skill/zone matching,
    sqft-based durations, auto twilight times, route optimization. Eos has
    assignment + drive-time buffers; the automation layer is the gap. Large.
11. **Editing-workflow pipeline** — Spiro/PhotoUp editor assignment + tracking as
    a Kanban stage separate from photographer pay. Medium-large.
12. **Floor plans / 3D** — CubiCasa-style integration or first-class floor-plan
    delivery; Matterport embeds (partially covered by `listing_media.py`). Medium.
13. **AI editing pipeline** (HDR merge, cull, sky replacement, virtual staging) —
    Fotello's core and the industry's center of gravity, but high build cost.
    Pragmatic path: integrate an external editor API (PhotoUp/Imagen-style
    round-trip) rather than building models. Large.
14. **Zapier app** — Eos has webhooks + API v1; a Zapier integration is mostly
    packaging. Medium.
15. **PostgreSQL backend** — already on the roadmap; needed past ~25 studios.

## Explicit non-goals (for now)

- Zillow Showcase / 3D Home integration (Zillow-owned moat, unobtainable) —
  position against it instead.
- Native app-store apps (PWA first).
- PostgreSQL/Redis multi-worker scaling (post-launch, tracked in docs/SCALE.md).

## Execution notes

- Follow `docs/AI_AGENTS.md` invariants: `studio_id` scoping everywhere, separate
  payment rails, append-only migrations, durable intents before provider I/O.
- Each wave item ships with tests; keep the 70% coverage floor and run
  `make test lint` (plus `make beta-smoke` for journey-affecting changes).
- Update CHANGELOG.md and README.md feature lists as items ship.
