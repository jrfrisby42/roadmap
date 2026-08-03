# Commercialization staging plan (companion to commercialization-read.md)

Build-sequenced plan for the phases in `commercialization-read.md`. Written 2026-08-03.
Reminder: Phase 0 is a VALIDATION activity, not an engineering task - the biggest risk is
treating it as a build. Engineering leads only from Phase 1.

---

## Phase 0 - how to start (mostly NOT engineering)

Goal: prove one or two real IT/Ops orgs feel the pain and would pay for the bundled
ticket <-> asset <-> service-history loop, BEFORE investing in platform work.

Checklist:
1. Positioning (half a page, no code). One sentence: who it's for + the pain + the wedge.
   Draft: "For internal IT/Ops teams, Flow + AssetHub is the one place where a ticket is linked
   to the physical asset it's about, work is planned against real capacity, and closing the ticket
   automatically writes the asset's service history - without a ServiceNow-sized deployment."
   List the 3 concrete problems it removes (asset context lost in tickets; no capacity view for
   IT work; service history scattered/manual).
2. Target list (J.R. owns). 2-3 orgs you can actually reach; WARMEST = anyone already touching
   AssetHub or who has asked about asset/ticket linking. Warm relationships beat cold reach for
   a validation experiment.
3. Problem interviews FIRST (no demo). 20-30 min each. Validate the pain before showing the tool:
   - How do you connect IT work to the specific asset today? (spreadsheet? nothing? ticket notes?)
   - How do you know if your IT team is over capacity? Do you plan it at all?
   - Where does an asset's service history live? Is it trustworthy?
   - What does the current gap cost you (time, audit pain, bad decisions)?
   If the pain is weak/absent, STOP - the wedge is wrong; do not build Phase 1.
4. Demo the closed loop (only for prospects whose pain is confirmed). Show: an item linked to an
   asset -> resolve it -> the ServiceEvent landing in AssetHub. The read integration + WRITE-1b are
   already LIVE on the `it` team, so the loop works today. For a clean demo, seed a demo team +
   demo assets (see "Phase 0 engineering enabler" below) rather than screensharing real `it` data.
5. Pricing probe. Name a number (a range is fine) and watch the reaction - you are testing
   willingness to pay, NOT collecting money. No billing infra needed for this.
6. Go/no-go signal (decide the bar in advance). Example: "2 of 3 confirm the pain, react
   positively to price, and ask for a pilot/timeline." That signal = green-light Phase 1. Anything
   less = iterate positioning or segment before building.

Phase 0 engineering enabler (small, optional, only if a live demo is needed):
- A seeded DEMO team (realistic IT/Ops items, a few linked assets) + a 5-step demo script, so the
  closed loop is shown intentionally and not against production data. This is seed data + a script,
  not new features. Est: under a day. Everything else in Phase 0 is conversations, not code.

Bottom line for Phase 0: the work is 5 conversations and a sharp pitch, optionally one seeded demo.
It is the cheapest, most decisive step and it gates everything below.

---

## Phase 1 - sellable to a design partner (current architecture; NO Postgres)

Ordered by leverage. Each ships on SQLite-per-team.

1. Part B - capability model + SERVER-SIDE enforcement (does double duty).
   - Client capability cascade (remove affordances when a capability is off) + the cascading
     capability tree in the Admin "Views & Access" tab (Part A Phase 1 already built the home for it).
   - Server 403 gates on the gated endpoints (sprints/planning/releases/etc.). THIS is the
     entitlement boundary every paid tier needs - the reason it leads.
   - Releases -> its own toggleable view; sprints-off removes the item Sprint field/history
     (decisions already locked).
   - Effort: the client cascade + tree is a few days; the server gates are the important, careful part.
2. SSO (SAML/OIDC). Table stakes for B2B. Layers on the existing HMAC-token auth as an identity
   front-door; does NOT need Postgres. Effort: medium (an OIDC/SAML front-door + user provisioning
   mapping). Prioritize by customer demand - the design partner may require it day one.
3. Litestream backups ON in prod. Generator + hooks exist; enablement is env + IAM + systemd only.
   Effort: low. Do before any paid data lives in the system.
4. Self-host the Tiptap editor bundles. Remove the esm.sh runtime dependency (embed like the PWA
   icons/logos). Effort: low-medium. Reliability/supply-chain hygiene for a paid product.
5. Minimal plan/licensing model + metering hooks. A per-team plan record (which capabilities/tier)
   + counters (seats, items, API calls). Manual invoicing is fine at first; the point is to have
   the data. Effort: low-medium; rides on Part B's capability set.

Phase 1 exit = you can run a paid pilot with a real entitlement boundary, backups, SSO if required,
and no CDN dependency in the edit path.

---

## Phase 2 gate - the Postgres / tenancy decision

Do NOT lead with Postgres (see the timing note in commercialization-read.md). Enter Phase 2 only
after Phase 0 validates and you commit to multi-tenant SaaS at scale. Then do it EARLY (before the
tenant count makes migration expensive). The ~591-test pytest suite is what makes it feasible.

The real decision is the tenancy model:

| | Row-level tenancy (single DB, `team_id` on every table) | Schema-per-team (one Postgres schema per team) |
|---|---|---|
| Cross-tenant queries (billing, analytics, admin console) | Native + easy | Hard (must union across schemas) |
| Isolation / blast radius | Weaker - one missed `team_id` clause = cross-tenant LEAK (security-critical) | Strong - isolation by construction |
| Migrations | One migration, all tenants | N migrations (or a loop), heavier ops |
| Connection/scale | Standard SaaS pattern; pools well | More schemas = more overhead at high tenant counts |
| Closeness to today's model | Different (today is DB-per-team = closest to schema-per-team) | Closest to today's SQLite-per-team isolation |

Recommendation: **row-level tenancy** IF cross-tenant analytics/billing/admin is a goal (it is, for
a licensing business) - but ONLY with a hardened, non-optional scoping layer: every query goes
through a helper that injects `team_id` (never hand-written WHERE clauses per call), plus a test
that fails if any endpoint returns cross-team rows. The cross-tenant admin/billing console is the
payoff that justifies accepting the row-level isolation risk. If cross-tenant queries are NOT
needed, schema-per-team is the lower-risk, closer-to-today path.

Migration approach (either model):
- Introduce a DB abstraction over the current hand-rolled sqlite3 calls (the `db()` helper + the
  `c.execute` sites) so the SQL surface has one home. Item blob `data TEXT` -> `jsonb`; FTS ->
  Postgres `tsvector`; the per-team tables (notifications, watchers, comments, planning_sessions,
  capacity_overrides, item_departments, item_assets) get `team_id`.
- Migrate the boot path OFF bulk `/api/all` onto the paginated/indexed endpoints FIRST or alongside
  (app-layer; Postgres does not fix this by itself).
- Run the pytest suite continuously through the migration; it is the safety net.
- Backfill live tenants with a one-time export/import per SQLite file into the new DB.

---

## Phase 3 - enterprise-ready
SOC2/audit trail, granular RBAC, SLA/support tiers, mobile apps, an integrations marketplace.
Driven by upmarket demand; do not pre-build.

---

## The one thing to do next
Phase 0, step 1-3: sharpen the one-line pitch and run problem interviews with 2-3 IT/Ops orgs. No
platform work until that returns a signal. If a live demo is wanted, the seeded-demo enabler is the
only code Phase 0 needs.
