# Commercialization read: Flow + AssetHub

An honest strategic read, written 2026-08-03. Candor over cheerleading. Caveat: this is written
with deep knowledge of Flow and only integration-contract-level knowledge of AssetHub (not a full
AssetHub product audit), and it cannot assess go-to-market / segment access - which is the real
determinant of viability.

---

## Verdict (short)
The software is credible and has a genuinely differentiated niche. It is NOT viable as a general
"Jira/Asana/Snipe-IT competitor." It IS potentially viable as a **focused, integrated IT/Ops suite**
for a segment the incumbents underserve. Viability is gated less by features (already demo-ready)
and more by (a) niche focus, (b) a substantial table-stakes commercial layer, and (c) distribution.

## The differentiated wedge (the actual asset)
Generic Kanban/roadmap and generic inventory are both commoditized and both have strong free/
incumbent competition. What is NOT commoditized is the COMBINATION Flow + AssetHub already has:

> Plan the work with real capacity  ->  link the ticket to the physical asset  ->  closing the
> ticket auto-writes to that asset's service history.

- Flow's differentiators: real capacity/resource scheduling (parallelResources, testing phases,
  delay accounting, derived pod capacity, the conflict engine), an intake portal -> work-item ->
  resolution loop, and cross-team (Dev sprints AND IT/Ops) by design.
- AssetHub's most differentiated capability is **external service-event ingestion** (the WRITE-1b
  contract) - which is worth the most when something is feeding it. That something is Flow.

The closed loop is hard for a pure PM tool OR a pure ITAM tool to match. Nearest competitors are
heavy ITSM suites (ServiceNow / Freshservice) - pricier and clunkier. That is the wedge.

## Flow standalone
Strong feature breadth (roadmap/Gantt/Kanban/sprints/releases/capacity/intake/reports/calendar/
rich-text/attachments/Jira sync/notifications/multi-tenant roles). But "another PM tool" is a
race to the bottom against well-funded incumbents with ecosystems, SSO, mobile apps, marketplaces.
Viable only by out-FITTING a segment, not out-featuring the field. The capacity + intake combo is
the reason to exist; plain PM is not.

## AssetHub standalone (with caveat)
Likely weaker standalone than as the asset-leg of the bundle. The ITAM market is real and funded
but mature and crowded, with strong FREE incumbents (Snipe-IT, GLPI) and established paid players
(Lansweeper, Freshservice, Asset Panda, ManageEngine) plus RMM tools that bundle inventory. Two
structural headwinds: buyers increasingly expect AUTO-DISCOVERY (agents/network scan/MDM sync) over
manual entry, and asset tracking is often bought as a FEATURE of a broader suite. To be viable
standalone, AssetHub needs a sharp edge: a vertical (schools/labs/healthcare/field service),
auto-discovery, or a standout custody/chain-of-custody + service-history workflow. Without one, it
competes against free. Its best asset (external service-event ingestion) is strongest bundled.

## Bundle vs separate
Recommendation: **sell the bundle, keep the code separate.**
- Bundle upside: same buyer (IT/Ops dept), one vendor/login/data model (removes integration
  friction - a top buyer complaint), higher switching cost/stickiness, higher ACV, and the "1+1=3"
  service-history loop already exists. It creates a niche neither tool can claim alone.
- Bundle cost: narrower TAM (need buyers who want both), double the maintenance + table-stakes
  infra for a small team, and suites are harder to sell (more stakeholders, longer cycle). Bundling
  two weak products does not make a strong one - it only works if the integrated story is the
  headline.
- The apps are already cleanly, loosely coupled over the API contract. Keep that (it lets you sell
  either alone and is good architecture). Merge the STORY and PRICING, not the code.

## The table-stakes gap (bigger than "small improvements")
The features are not what is missing for commercial. These are, and they are substantial:
- Server-side ENTITLEMENT enforcement (today gates are UI/rail/route only; endpoints are open -
  a licensing boundary that is not server-enforced is not a boundary). Part B starts this.
- SSO (SAML/OIDC) - table stakes for B2B.
- Real backups / DR (Litestream generator exists but is NOT enabled on prod).
- Self-serve billing + usage metering.
- Audit/compliance path (SOC2), granular RBAC, uptime/support, docs/onboarding.
- Self-host the Tiptap editor bundles (editing currently depends on esm.sh at runtime - a CDN in
  the critical path is a reliability/supply-chain risk for a paid product).

## Staged path (validate first, then build)

### Phase 0 - VALIDATE (before building any infra)
Find one or two real IT/Ops orgs (ideally already touching AssetHub) and test whether the LINKED
ticket<->asset<->service-history story makes them lean in and pay. A Postgres migration or SSO build
is weeks of work wasted if the wedge does not sell. This single experiment outranks any feature.

### Phase 1 - SELLABLE TO A DESIGN PARTNER (current architecture; NO Postgres needed)
- Part B: capability model + SERVER-SIDE 403 enforcement (the entitlement boundary).
- Enable Litestream backups on prod (DR).
- Self-host the Tiptap bundles.
- SSO (SAML/OIDC) - layers on the current HMAC-token auth as an identity front-door; does NOT
  require Postgres.
- A minimal plan/licensing model + metering hooks (manual invoicing is fine at first).
All achievable on SQLite-per-team. This is enough to run a paid pilot.

### Phase 2 - PLATFORM FOR SCALE (the Postgres move - when committing to SaaS-at-scale)
- Migrate SQLite-per-team -> single Postgres, ROW-LEVEL tenancy (team_id on every table + a
  hardened scoping layer), item blob -> jsonb, FTS -> Postgres tsvector. The ~591-test pytest suite
  substantially de-risks this (it will catch regressions a hand migration would otherwise ship).
- Migrate the boot path OFF bulk /api/all onto the paginated/indexed endpoints (this is an
  APP-LAYER fix; Postgres alone does NOT fix the /api/all ceiling - see the Postgres note below).
- Cross-tenant admin/analytics/billing console (enabled by the single DB).
- Multi-node app + managed infra (RDS, connection pooling).

### Phase 3 - ENTERPRISE-READY
SOC2/audit, granular RBAC, SLA/support tiers, mobile apps, an integrations marketplace.

## Postgres timing - honest note
Postgres is real and probably necessary IF the destination is multi-tenant SaaS at scale with
cross-tenant analytics/billing. But it is NOT the first move, for four reasons:
1. It fixes less than it seems. The /api/all boot ceiling is an APP design issue (loading the whole
   team into memory), not a SQLite limitation - Postgres alone does not fix it. SSO, entitlement
   enforcement, and backups do NOT depend on Postgres and can ship on the current stack.
2. It trades away a real strength. Two-file + SQLite + scp-deploy = very low operating cost and
   complexity, which is an ASSET for a bootstrapped niche product, not just a limitation.
3. The tenancy-model decision is the crux and is security-critical. Row-level tenancy (team_id
   everywhere) is the standard SaaS approach but a single missed scoping clause is a cross-tenant
   data leak. Schema-per-team preserves isolation but loses the cross-tenant-query benefit. This
   needs a deliberate decision, not a default.
4. Timing cuts both ways. It is the most invasive change, so doing it BEFORE many live tenants
   exist is far cheaper than after (migrating live tenant data across a DB-model change is painful).
So: do NOT lead with Postgres, but if Phase 0 validates and you commit to SaaS-at-scale, do it
EARLY in Phase 2, before the customer count makes migration expensive. The pytest suite is the
green light that makes it feasible.

## Bottom line
Keep hardening + small improvements WILL get the software to sellable quality - but that produces a
good product, not a viable business. The deciding moves are: pick the bundled IT/Ops wedge (do not
fight Jira or Snipe-IT head-on), build the table-stakes layer (bigger than polish), and - cheapest
and first - validate willingness-to-pay with one real IT/Ops org before investing in platform work.
