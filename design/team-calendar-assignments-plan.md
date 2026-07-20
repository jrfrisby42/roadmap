# Team Calendar, Assignments & Capacity Management — Design Plan

> Status: **PLAN ONLY — not yet built.** Reviewed against the current codebase; approved
> approach, implementation deferred. Author: planning session 2026-07-20.
> Greenlit as a **full epic, built incrementally** (Phase 0 → 3, each its own PR + deploy).

## Context

Flow today is owner-centric: **owner** = a capacity-pool label (item field `p.dev`, from the
`developers` config), with an abstract per-owner number `ownerCapacity[name]` and
per-(owner,date) `capacity_overrides` (capped at the base). `assignee` is a login username
that is currently **inert for capacity**. There is **no user→owner membership**. This
feature adds non-ticket work ("assignments" — PTO, Training, On Call, etc.), makes them
reduce effective capacity, and adds a **Team Calendar** view — making Flow a
resource/manager planning tool alongside project planning, without cluttering the Gantt.

### Decisions locked with the user
- **User model:** "Users" = existing **login accounts**; add a **user→owner membership**
  (a user belongs to one or more owner-pods; pods are multi-person). Membership drives the
  calendar roster and defaults an assignment's owner.
- **Impact units:** **absolute** numbers subtracted from owner capacity (PTO = -1.00,
  Training = -0.50), per spec.
- **Scope:** the **full epic**, **incrementally** — Phase 0 → 1 → 2 → 3, each its own PR +
  deploy + verification.
- **Conflicts: warn everywhere** — blocking/exclusive/capacity surface as **warnings the
  user can override**, never hard blocks (keeps `update_project`/planning untouched).

## Key architecture findings (from code mapping)
- **Single capacity choke point:** `getEffectiveCapacity(owner,date)` (`roadmap.html:6020`)
  + `get_effective_capacity` (`server.py:5196`) are read by every capacity surface (Gantt
  bars `buildCapacityGraphHtml:2923`, "X/Y" cap calendar `renderCapCal:6077`, dashboard
  heatmap `renderCapacityHeatmap:13307`, conflicts `computeConflictingIds:3191`). Inject
  assignment impacts **here** and everything updates.
- **Override ceiling must relax:** `_validate_override_capacity` (`server.py:5213`) rejects
  `override > base` (tested: `test_override_cannot_exceed_default`). The spec needs
  above-base overrides (+contractor). Redesign the model (below) + update that test.
- **Admin is a self-contained modal** (`adminModalBg`, `roadmap.html:1792`) reached in Flow
  via `frzAdmin`→`openAdminPanel` (`:18260`); per-tab renderers target inner list IDs, so
  they work verbatim inside a routed page. Full-page conversion ≈ 5 touch points; keep the
  modal for the classic (`?classic=1`) surface.
- **Statuses page is the template** for the Assignments admin page (`renderStatusList:6911`
  — drag-reorder, checkbox toggles, one-default radios `setDefaultStatus:7315`, system/lock
  `isProtectedStatus:7018`, pencil/trash).
- **My Home (`frzHome`, `roadmap.html:16803`) is the template** for a Flow-owned view;
  Planning/My-Home sub-tabs (`frz-plan-tabs`) for Month/Week/Timeline modes.
- **Storage patterns:** config-backed collection (boards: `_read_boards:3296`, `GET/PUT
  /api/boards`) for `assignmentTypes`; relational table (`capacity_overrides:587`) for
  `assignments`.
- **Recurrence engine** (`spawn_recurrence:6026`, `PERIOD_DAYS`, advance/skip/idempotency/
  `*_parent` chain) ports to assignments; **round-robin rotation is net-new**.

## Data model
- **assignmentTypes** — config-backed collection (dedicated `GET/PUT /api/assignment-types`,
  NOT in `VALID_KEYS`, mirroring boards). `{id, name, category, icon, color, active, system,
  is_default, blocks_work, exclusive, capacity_impact, allow_tickets, allow_assignments,
  show_on_calendar, display_order}`. Seed the 12 spec defaults in `init_team_db` +
  `_migrate_config_keys` backfill. One-default + system/lock enforced client-side (status-flag
  pattern) + light server validation (reject deleting a `system` type).
- **assignments** — new per-team table (mirror `capacity_overrides`): `id, type_id, owner,
  username, start_date, end_date, description, recurrence, notes, created_by, created_at,
  updated_at` (+ index on `(owner,start_date,end_date)`; `recurrence_parent` for Phase 3).
- **Membership** — add `owners: [ownerName,...]` to each user record in the `users` config
  (distinct from the single `ownerFilter` scoping field; leave that as-is). Editable in
  Admin; drives calendar roster + default owner on assignment create.

## Capacity engine redesign (Phase 1)
- **Effective formula:** `effective = (override ?? base) − Σ(active assignment impacts for
  owner+date)`, floored at 0. Keep overrides as absolute replacements but **remove the ≤base
  ceiling** (spec ex. 4: contractor override above base). Update `_validate_override_capacity`
  + its test; drop the client `capInp.max`/6327 ceiling.
- **Impacts source:** Σ impact per (owner,date) from `assignments` × the type's
  `capacity_impact` (active types, in date range). In-memory `_assignmentImpacts[owner][date]`
  at boot (mirror `_capOverrides`/`loadAllCapOverrides` `:5998/15361`); server mirror in
  `get_effective_capacity`.
- **Inject** in the two choke points → all capacity surfaces + conflicts update automatically.
- **Breakdown tooltip:** Base / each assignment `-impact` / Effective on capacity cells.

## Conflict engine — advisory only (Phase 1)
Pure functions returning warnings (never block a save):
- Assignment overlapping a **blocking**/**exclusive** assignment for the same user → warning
  in the assignment editor.
- Setting an item's **assignee** to a user with an overlapping blocking assignment →
  non-blocking warning near the assignee field. `update_project` success path unchanged.
- Capacity overage reuses the existing conflict styling.

## Backend endpoints (`server.py`, additive; capacity_overrides/comments template)
- `GET/PUT /api/assignment-types` (boards pattern; PUT `require_role("admin")`).
- `GET /api/assignments?owner=&user=&date_from=&date_to=` (`require_auth`); `POST/PUT/DELETE
  /api/assignments[/{id}]` (`require_role("admin","editor")`, `write_audit`).
- `GET /api/assignments/impacts` for the client boot load (or fold into `/api/all`).
- Reuse `require_role` (`:445`), `write_audit` (`:930`), `db(team)`. No new deps.

## Frontend
- **Icons:** flat `IC.teamCalendar` (calendar-days) + clipboard (`IC` at `roadmap.html:16591`);
  16×16 inline, currentColor, no gradients/shadows.
- **Admin → full page (Phase 0):** route `/admin` as a Flow-owned view (container in `build()`
  `:16793`; add `admin` to the four route regexes + a `setView` branch; repoint `#frzAdmin`
  `:18260`). Keep the classic modal. Add the **Assignments** tab (`renderAssignmentsList`
  modeled 1:1 on `renderStatusList`).
- **Team Calendar (Phase 2):** Flow-owned like `frzHome` — `frzCalendar` container, pinned
  rail button (`renderRail` `:17019`), route `/team-calendar/{month|week|timeline}`
  (`state.calTab`, add to the four regexes + `setCalendar()` like `setHome` `:17431`).
  Owner→member-Users, each user's assignments + tickets, per-owner effective-capacity header,
  three modes. Filters: Owner/Assignee/Type/Project reuse existing chips (`CHIPS` `:16611`);
  Department/Assignment-Type/Capacity are new chips or a custom filter row (My-Home style).
- **Gantt:** no visual change — bars/tooltips already read `getEffectiveCapacity`, so they
  reflect impacts automatically (spec's "don't add assignment bars to Gantt").

## Phasing (each = own PR + deploy + verification)
- **Phase 0 — Admin full page + icons** (contained refactor; unblocks the 11-col table).
- **Phase 1 — Types + Assignments CRUD + Capacity redesign + advisory conflicts** (the core).
- **Phase 2 — Team Calendar** (grouping, modes, filters, breakdown tooltips).
- **Phase 3 — Recurring assignments + on-call rotations (round-robin, net-new), Team Health
  dashboard, forecasting.**

## Concerns / tradeoffs to accept
- **Capacity is a core, thinly-tested subsystem** — phase-math + render path have no pytest
  coverage (frontend-only). Verify in-browser + add tests for the new effective formula.
- **Impact = absolute units** assumes owners are sized so 1 person ≈ the impact number;
  single-person owners (cap 2) under-reduce unless impacts are tuned (accepted).
- **Membership is new per-user state**; if Option B (global accounts) lands later, keep the
  assignment→user reference as the username string (stable across that refactor). Don't run
  this epic concurrently with Option B.
- **Scope:** genuinely large (weeks). Phase gates keep it shippable + reversible.

## Verification (per phase)
- `pytest` green incl. updated `test_capacity.py` (above-base overrides allowed; impacts
  subtract) + new `tests/test_assignments.py` (types CRUD + one-default/system guards;
  assignment CRUD + role gates; effective-capacity math with impacts; warnings).
- JS syntax on both `<script>` blocks; `ast.parse` on `server.py`.
- Local browser E2E on a throwaway team (chrome-devtools): define types; add a PTO
  assignment; confirm owner capacity drops on Gantt bar + cap calendar + dashboard heatmap
  with a correct breakdown tooltip; confirm warnings appear (non-blocking); open Team
  Calendar and see Owner→Users with assignments + tickets.
- Deploy per phase (scp + restart); bump `APP_VERSION` (minor per phase).
