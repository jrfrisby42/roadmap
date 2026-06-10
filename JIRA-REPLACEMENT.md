# JIRA-REPLACEMENT.md

A phased plan to evolve Frazil Roadmap from a Jira **mirror** into a Jira
**replacement** — the team's system of record for all work items, not just the
Gantt-level roadmap.

> Status: planning. Nothing here is built yet. See `CLAUDE.md` / `ARCHITECTURE.md`
> for how the app works today.

---

## 1. Vision

Today the tool *reflects* Jira (forward-only status sync, feature-flag pull). The
goal is to make it the team's system of record: every Feature, Enhancement, Task
and Subtask tracked here, in a hierarchy, with a fast list/table view for the
granular work that never reaches the Gantt — and eventually a ticket-aware copilot
that helps author work and hand it to Claude Code.

**The conceptual shift:** mirror → **system of record**. Most of the work below is
"what a system of record needs that a mirror doesn't."

### Scoping decisions (agreed)
- **Scale:** potentially **thousands** of items → the data model must become
  queryable; "load everything into memory" no longer holds.
- **Process:** **hybrid**, tracked primarily by **story points** + **sprints**.
  No time-tracking / worklogs (explicitly out of scope).
- **Gantt membership is per-type:** each item type has a **"show on Gantt"** flag.
  Only show-on-Gantt types are scheduled and consume capacity; everything else is
  pure tracking in the List view. (This is how we keep thousands of tasks from
  polluting the Gantt / capacity engine.)
- **Owner vs. assignee:** `owner` stays a **generic grouping** whose meaning each
  team defines (kept ambiguous on purpose so other teams can adopt the tool);
  **Frazil uses owner = Pod**. `assignee` = the **individual**, sourced from `users`
  via **multi-Pod membership** (the existing `ownerFilter`, made multi-valued — §4).
  **Owner-first** flow: choosing an owner filters the assignee list to that Pod's
  members. No new "Pods" config — the owner list + user membership already model it.
- **Capacity:** the existing capacity engine is **unchanged** — it operates only on
  the bigger (show-on-Gantt) items, keyed on `owner` (now the Pod). No schedule
  roll-up from children.
- **Access:** whole team sees and edits everything (most users have **empty** Pod
  membership = unscoped). admin/editor/viewer is fine as-is; multi-Pod membership
  doubles as an optional **Pod-scoping** lever for later (§4).
- **Attachments:** desired, ideally via **ShareBox** — *nice-to-have, not required*.
- **Cutover:** **transition period** — Jira and the tool run side by side.
- **Copilot:** **authoring first** (help write/groom tickets); Claude Code handoff later.
- **Item keys:** human-readable, prefix **configured per product in Admin → Projects**.
- **Reporting:** parked (don't forget) — see §14.
- **GitHub / GitHub Actions / machine tokens:** parked for a later phase — see §14.

### Guiding constraints (don't break these)
- Two-file deploy (`server.py` + `roadmap.html`), no build step, no frontend framework.
- Config-driven statuses/types (never hardcode status names — `statusIs*` flag maps).
- Server-side role gating on every mutation.
- New runtime deps only with a real reason (precedent: `boto3` for SES).

---

## 2. Current state (what we can build on)

| Capability | Today |
|---|---|
| Items | JSON blobs in `projects(id, data)`, **no field indexing** |
| Load model | `GET /api/all` returns the **whole team** into `projects[]` in memory |
| Types | `types` config (Feature, Enhancement, Maintenance) |
| Statuses | `statuses` config + `statusIs*` flag maps |
| Relationships | `parent`/`child` (containment, single-level) **XOR** `requires`/`dependent` |
| Owner | individual developer; capacity keyed on it (`ownerCapacity`, `capacity_overrides`) |
| Feature flags | `featureFlags` (manual) + `jiraFeatureFlags` (pulled) — **reuse, don't rebuild** |
| Views | Gantt, Kanban, Planning, Dashboard |
| Detail | Item Page (`/?item=`), comments, activity feed, audit log |
| Estimate | `dueWeeks` / `testWeeks` / `parallelResources` (capacity) |
| Roles | viewer / editor / admin (+ owner-scoped editors via `ownerFilter`) |
| Email | SES via boto3 + instance role |
| Jira | forward-only status sync, FF pull, ADF description sync |

---

## 3. Data model & queryability (Phase 1 — do first, highest risk)

At thousands of items, the JSON-blob/no-index model and full-team `/api/all`
payload fall over. **This is the prerequisite everything else hangs off.**

### 3.1 Promote hot fields to indexed columns
Keep the JSON blob as source of truth for arbitrary fields, but mirror queryable
ones into real columns, written on every create/update and backfilled by a
boot-time migration (idempotent, like `_migrate_config_keys`):

```
ALTER TABLE projects ADD COLUMN item_key     TEXT;     -- e.g. FRAZ-142
ALTER TABLE projects ADD COLUMN type         TEXT;
ALTER TABLE projects ADD COLUMN status       TEXT;
ALTER TABLE projects ADD COLUMN parent_id    INTEGER;  -- hierarchy
ALTER TABLE projects ADD COLUMN product      TEXT;
ALTER TABLE projects ADD COLUMN owner        TEXT;     -- Pod
ALTER TABLE projects ADD COLUMN assignee     TEXT;     -- individual
ALTER TABLE projects ADD COLUMN reporter     TEXT;     -- creator
ALTER TABLE projects ADD COLUMN priority     INTEGER;
ALTER TABLE projects ADD COLUMN rank         TEXT;     -- backlog ordering (§10)
ALTER TABLE projects ADD COLUMN story_points REAL;
ALTER TABLE projects ADD COLUMN sprint_id    TEXT;
ALTER TABLE projects ADD COLUMN archived     INTEGER DEFAULT 0;  -- soft delete (§10)
ALTER TABLE projects ADD COLUMN updated_ts   TEXT;     -- optimistic concurrency
CREATE INDEX idx_projects_parent  ON projects(parent_id);
CREATE INDEX idx_projects_status  ON projects(status);
CREATE INDEX idx_projects_product ON projects(product);
CREATE INDEX idx_projects_sprint  ON projects(sprint_id);
CREATE UNIQUE INDEX idx_projects_key ON projects(item_key);
```

### 3.2 Query/search/paginate endpoint
```
GET /api/items?product=&type=&status=&owner=&assignee=&sprint=&parent_id=&archived=&q=&sort=&page=&page_size=
```
Returns a page + total count. Backs the List view and the copilot's retrieval.
`q` = LIKE over key/name/notes (fine at this scale; FTS5 later if needed).
**Escape hatch for un-promoted fields:** filter via SQLite `json_extract(data, '$.field')`
(optionally a generated column + index) so users can query arbitrary blob fields.

### 3.3 Scope down `/api/all`  ⚠ contract change
`/api/all` is relied on heavily (CLAUDE.md #12). Plan: it returns config + the
**bounded** set the Gantt/Kanban need (show-on-Gantt items), **not** every subtask.
List view + detail use `/api/items` and `/api/items/{id}`. Frontend and backend
move in lockstep here — this is the riskiest single edit.

**Effort: L. De-risk first.**

---

## 4. New & changed fields

| Field | Meaning | Notes |
|---|---|---|
| `item_key` | Human-readable key | Per-product prefix (§5) |
| `reporter` | Who created the item | Auto-set on create; admin-editable |
| `assignee` | Individual doing the work | Independent of owner; reassigned freely |
| `owner` | The **Pod** | Meaning shifts from "individual" → "Pod" |
| `story_points` | Estimate | Primary tracking metric; separate from weeks/capacity |
| `parent_id` | Hierarchy parent | §6 |
| `sprint_id` | Sprint membership | §8 |

**Owner / assignee model (kept generic; reuses the existing user↔owner tie-in):**
- `owner` is **unchanged** — the capacity-bearing grouping field; meaning is
  team-defined. **No owner migration.** Frazil populates the owner list (`developers`
  config) with **Pods** (the name is cosmetic).
- `assignee` (new field) is sourced from **`users`** — specifically users whose **Pod
  membership includes the item's `owner`**. No separate people config needed.
- **Membership = multi-valued `ownerFilter`.** Today `ownerFilter` is a single owner
  string that view-scopes an editor; we make it a **list** so a user can belong to
  several Pods (people float between squads). One concept serves both **assignment
  candidacy** and (optional) **view-scoping**.
  - *Migration (Phase 2):* existing `ownerFilter` strings → single-element lists;
    update its consumers (view-scope logic, modal owner-lock, 📌 badge) to "any of my
    Pods". **Empty list = unscoped = sees/edits everything** (the default for the team).
- **Owner-first** is the primary flow — with multi-Pod membership an assignee no
  longer uniquely implies a Pod: pick owner → assignee dropdown filters to that Pod's
  members; assignee→owner autofill only when the user is in exactly one Pod; override
  always allowed. Fallback: empty Pod (or assigning outside it) → dropdown falls back
  to all users rather than blocking.
- Capacity stays keyed on `owner` (per-Pod, for Frazil) and is **independent of how
  many Pods a person belongs to** — **no capacity-engine change**.

**Per-type config (Admin → item types)** gains:
- `showOnGantt` (bool) — schedulable + capacity-consuming, or pure tracking.
- hierarchy nesting rules (which types nest under which — §6).

---

## 5. Item keys (per-product, Admin → Projects)

- Each product (`products` config — Fraznet, HubSpot, …) gains a **`keyPrefix`**
  (e.g. `FRAZ`, `HUB`), edited in **Admin → Projects**.
- New items get `item_key = {keyPrefix}-{n}`, with a per-product counter
  (a `key_counters` table — safer than `MAX()+1` under concurrent writes).
- Keys are **immutable** once assigned; shown everywhere (list, board, Gantt,
  Item Page, copilot, Claude Code briefs).
- On product move: **keep the original key** (recommended; simpler than re-keying).
- Preserve any `jiraKey` as a cross-reference during transition (§11).

**Effort: S–M.**

---

## 6. Hierarchy (typed, N-level)

Today `parent`/`child` is single-level and **mutually exclusive** with
`requires`/`dependent`. For Jira parity:
- **N-level tree** via `parent_id` (Feature → Enhancement → Task → Subtask).
- **Type rules in config** (which types nest under which), edited with the types.
- **Relax the mutual-exclusion rule** — an item needs **both** a parent *and*
  dependency links (Jira does). `parent` = hierarchy; `requires` = scheduling;
  orthogonal from now on.
- **Display roll-ups (optional):** child count + % done for the list/detail. Note:
  **schedules do NOT roll up** (set independently, per the capacity decision) — so
  roll-ups are a display nicety, not a scheduling mechanism.
- Tree ops: reparent, cascade-vs-orphan on delete (confirm UX), cycle prevention.

**Effort: M.**

---

## 7. List / table view (core ask)

A new view mode beside Gantt/Kanban/Planning/Dashboard:
- Virtualized, server-paginated table backed by `/api/items`.
- **Tree grouping** (expand/collapse) + flat/filtered modes.
- Columns: key, type, status, assignee, owner(Pod), points, sprint, parent,
  updated — configurable.
- Inline edit, **multi-select bulk actions** (status / assignee / owner / sprint /
  points / parent).
- Quick-add (keyboard-first); saved filters (§10).

**Effort: M.**

---

## 8. Story points & sprints

- **Story points** field (separate from the weeks/`parallelResources` capacity
  model, which stays for the Gantt). Primary tracking metric.
- **Sprints**: a `sprints` table per team `(id, name, start, end, state, goal)` +
  `sprint_id` on items. Backlog = no sprint; board filters to the active sprint.
  Complements the existing Planning Sessions (kept for Review/Release ceremonies).
- **Reporting** (velocity/burndown) is **parked** — see §14.

**Effort: M.**

---

## 9. Notifications

**v1 (now in scope): Slack.**
- **Incoming Webhook → one global channel.** Config via `SLACK_WEBHOOK_URL`
  (env, like the SES/Jira settings) — posts via `urllib`, no new dependency.
- **First trigger: item creation.** (More events — status changes, mentions —
  to follow.)

**Later:**
- More Slack events; **email** notifications (build on the SES sender) for
  assigned-to-you / mentioned / watched-item-changed.
- **@mentions + watchers** (parse mentions against users; in-app activity center
  already exists).
- **Notification preferences** (per-user opt in/out, digest vs. immediate) — becomes
  important once email + per-event Slack land, to avoid a firehose at scale.

**Effort: S (Slack v1) → M (full notification layer).**

---

## 10. Parity polish

- **Soft delete / archive + restore** (`archived` flag) instead of hard delete — a
  system of record shouldn't destroy history (today we cascade-delete comments/
  activities).
- **Backlog ranking** (`rank`, lexorank-style) for drag-to-prioritize grooming —
  integer `priority` alone gets clumsy in a hybrid-scrum backlog.
- **Server-side saved filters** — promote saved views from per-user `localStorage`
  to a shareable, config-backed resource (the JQL-equivalent).
- **Optimistic concurrency** on writes — version-check via `updated_ts` so
  concurrent inline/bulk edits don't silently clobber (we already hit one
  lost-update race in `update_project`).
- **Bulk CSV import/export** round-trip (extend the HubSpot importer).
- **Per-type workflows / status sets** (optional) — different statuses per item
  type + allowed-transition rules.
- **Labels / components / custom fields** (nice-to-have).

**Effort: M, incremental.**

---

## 11. Migration & transition (both run)

1. **Import**: bulk-pull *all* Jira issues + hierarchy + comments + history (extend
   the existing Jira pull). Map types/statuses/parents; preserve `jiraKey`.
   **Fidelity is the hard part:** comment authorship + timestamps, issue-link
   types, sprint history, watchers, and **pulling attachments out of Jira into
   ShareBox** (§13) are each real work — budget for them explicitly.
2. **Coexistence (transition):** simplest split —
   - New work is created in the tool (native key).
   - Legacy items keep their Jira link; status syncs **bidirectionally** so neither
     side goes stale. (`jiraEnabled` already gates sync.)
3. **Cutover:** set Jira read-only, **disable sync**; the tool is authoritative.

> Open: bidirectional sync during transition vs. tool-authoritative-for-new /
> Jira-authoritative-for-legacy?

**Effort: M–L.**

---

## 12. Rollout & testing

- **Feature-flag the new views per team** (a config flag) so the List view /
  hierarchy roll out without disrupting current users mid-transition.
- **Tests per phase** — we now have a pytest suite; the Phase 1 data-model + API
  change especially needs coverage (migration backfill, query filters, the
  `/api/all` slimming, key generation).
- Migrate live DBs carefully: backfill performance + idempotency on thousands of rows.

---

## 13. Attachments via ShareBox (optional)

ShareBox already has S3-backed storage — don't rebuild it here.
- **Lightweight (start):** an attachments field storing **ShareBox links/refs**;
  upload in ShareBox, attach the link.
- **Deeper:** a ShareBox API integration (needs an auth/API contract between the
  two apps).
- Keep S3/boto storage code out of roadmap — delegate to ShareBox.

**Effort: S (links) → M (API). Defer; not required.**

---

## 14. Parking lot (captured, deferred)

- **GitHub + GitHub Actions integration** — bidirectional dev linkage:
  (a) create branch/PR from a ticket, (b) PR/commit → ticket linking +
  auto-transition (smart commits, "In Review on PR open / Done on merge"),
  (c) CI/Actions status on the ticket (deploy success → Released). Across one or
  more orgs/repos. **Parked for a later phase.**
- **Machine / service API tokens** — separate from user bearer tokens, so CI and
  GitHub Actions can post updates back (status/release). Prerequisite for the
  GitHub work above. **Parked.**
- **Reporting** — velocity, burndown, sprint report, created-vs-resolved, control
  charts. **Parked, not forgotten.**
- **Time tracking / worklogs** — explicitly **out of scope** (points-based).

---

## 15. Phase order

1. **Data model + query API + item keys** (§3–§5) — foundation; de-risk first.
2. **List view + hierarchy + new fields** (§4, §6, §7).
3. **Story points + sprints** (§8); **Slack v1 (item-creation)** (§9).
4. **Migration + transition coexistence** (§11).
5. **Parity polish**: soft-delete, ranking, saved filters, concurrency, bulk CSV (§10).
6. **Copilot authoring** (below), then handoff.
7. **Attachments via ShareBox** (§13) — slot in whenever; optional.
8. **Parking lot** (§14): GitHub/Actions + machine tokens, reporting.

---

## 16. Copilot (authoring-first) + Claude Code handoff

### Phase A — ticket authoring (priority)
A chat with context of the team's items (via `/api/items` retrieval, not stuffing
everything into context). It can draft tickets, write acceptance criteria, split a
Feature into Enhancements/Tasks, suggest type/assignee/owner/points, find duplicates.
- Claude API. Use the `claude-api` skill patterns — **prompt caching of the system
  prompt + retrieved ticket context is essential** for cost/latency at this scale.
- **Dependency decision:** Anthropic SDK (nicer: caching, streaming) vs. hand-rolled
  `urllib` (matches Jira, zero new dep). Recommend SDK; flag for sign-off.

### Phase B — Claude Code launching pad
A button on an item that assembles a **structured brief** — item + children +
acceptance criteria + dependency links + linked code areas — into a copy-paste
prompt or a generated `TASK.md` the dev opens Claude Code against. Start with
markdown export; deep link / MCP later.

**Effort: M (authoring) → M (handoff).**

---

## 17. Risks & open questions

- **`/api/all` contract change** is the riskiest single edit — frontend + backend
  must move together.
- **In-memory `projects[]` assumptions** are sprinkled through `roadmap.html`; the
  query path coexisting with the Gantt's in-memory model needs care.
- **Two estimate models** coexist — weeks/`parallelResources` drive the Gantt;
  story points drive sprint tracking. Keep them clearly separated.
- **Owner/assignee (resolved):** `owner` generic + unchanged; `assignee` sourced from
  `users` via **multi-valued `ownerFilter`** Pod membership; **owner-first** flow;
  empty membership = see-all. Phase-2 task: migrate `ownerFilter` string→list and
  update its consumers (view-scope, modal lock, badge).
- **Copilot dependency**: SDK vs. urllib (sign-off).
- **Transition sync direction**: bidirectional vs. split authority?
- **Per-type workflows**: needed at launch, or later?
