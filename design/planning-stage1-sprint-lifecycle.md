# Plan (DRAFT, for review) - Planning Stage 1: Sprint History Integrity + Sprint Lifecycle Management

Status: **BUILT + shipped in 4.39.0** (all locked decisions below implemented). Snapshot
fix + backfill migration + edit/discard lifecycle + Discarded state + enhanced audit.
Tests in `tests/test_sprint_lifecycle.py`.

---

## 1. Problem statement

Two related gaps in the Sprints feature today:

**A. Sprint history is not durable (the visible bug).**
`sprintStats(sp)` (`roadmap.html:20161`) computes a sprint's Items and Points from its
**current** membership - `sprintItems(sp.id)` = live `projects` where `p.sprintId===sp.id`.
For a *completed* sprint, carried-over items have already been moved off to the next
sprint, so a finished sprint that carried 1 item over shows **0/0 items, 0/0 points** even
though `carryOver` is 1. Once an item is reassigned, deleted, or archived, the record of
what that sprint actually contained is gone. The Sprints tab is therefore untrustworthy as
history.

**B. There is no sprint lifecycle management.**
Sprints can be created (Plan tab), started (`startSprint`), and completed
(`completeSprint`), but there is **no way to edit** a sprint's name/dates/goal after
creation and **no way to delete or discard** a sprint. `PUT /api/sprints` only accepts the
three states `Planned | Active | Completed` (`server.py:_SPRINT_STATES`, line 3439).

---

## 2. Goals

1. Capture a **durable snapshot** of a sprint's contents at completion so the Sprints tab
   reports true historical Items/Points/carry-over regardless of later item churn.
2. **Backfill** that snapshot for existing completed sprints so history isn't blank on day one.
3. Add **edit** (name, dates, goal) and **discard** lifecycle actions, with the
   user-specified discard behavior (below) and proper audit.

Non-goals for Stage 1: changing the planning-session commit flow, changing how sprints are
started/completed mechanically, or touching capacity math.

---

## 3. Data model changes

### 3.1 Sprint snapshot (fixes problem A)

Add a snapshot to the sprint object (config-backed `sprints` array) written **at completion
time** and read by `sprintStats` in preference to live membership:

```
sp.snapshot = {
  takenAt: "<ISO date>",
  items: [ { id, key, name, points, outcome } , ... ]   // outcome: completed | carried-over | returned-to-backlog
}
```

- Storing **key/name/points/outcome per item** (not just ids) makes the snapshot resilient
  to later item deletion and preserves the points as they were at completion (a later
  re-estimate won't rewrite finished history). This is my review finding #5.
- `sprintStats(sp)` becomes: **if `sp.snapshot` exists, derive total/done/committed/completed
  from the snapshot; else fall back to the current live computation** (so Planned/Active
  sprints, which have no snapshot yet, are unchanged).
- `doCompleteSprint` (`roadmap.html:20126`) already iterates every member item and knows
  each one's outcome (`completed` / `carried-over` / `returned-to-backlog`). It will build
  and attach `sp.snapshot` right before `finalize()` writes state=Completed.

### 3.2 New sprint state: `Discarded`

- Add `"Discarded"` to `_SPRINT_STATES` in `server.py:3439` (otherwise `PUT /api/sprints`
  422s the whole array). This is review finding #2.
- Add a `Discarded` entry to `SPRINT_BADGE` (`roadmap.html:17014`) - a neutral/muted color
  (e.g. background `#F3F1F5`, text `#6B5B73`), **no indigo**.
- Discarded sprints are **kept, not deleted** (state, not row removal) so history/audit
  survive. They render on the Sprints tab with the Discarded badge.

---

## 4. Behaviors

### 4.1 Edit sprint (name / dates / goal)

- Available on Planned and Active sprints (and arguably Completed - see open question, but I
  propose **Planned + Active only**; a completed sprint's dates are historical).
- Small modal (reuse the `.frz-modal` pattern from `completeSprint`): name, goal, start
  date, end date. Validate end >= start; name non-empty.
- Saves via the existing `saveSprints(...)` -> `PUT /api/sprints`.
- **Audit:** editing an **Active** sprint's dates is the sensitive case (it moves the goal
  posts mid-flight); the spec asks for this to be logged prominently. See open question B.

### 4.2 Discard sprint

Discard applies to **Planned sprints only** (decision 4). An Active sprint must be
**Completed** - Complete is its only exit; there is no Discard action on Active or Completed
sprints.

Discard = set `sp.state='Discarded'` (keep the row), then release its items per the spec as
written (decision, reverting the earlier Deferred idea). For every `p` with
`p.sprintId===sp.id`:
- `p.sprintId = ''` (item leaves the discarded sprint).
- **No status change** - the item's status is left exactly as it was.
- close the item's open `sprintHistory` entry (outcome `returned-to-backlog`), matching the
  existing remove-from-sprint bookkeeping, and log a "Sprint planning:" activity.

A confirmation modal states the consequence explicitly before applying ("Discard {name}?
Its N item(s) will be removed from the sprint (returned to the backlog). This cannot be
undone.").

### 4.3 Where the controls live

Add **Edit** (Planned + Active rows) and **Discard** (Planned rows only) affordances on the
Sprints-tab rows (`renderSprintsTab`, `roadmap.html:20173`) - a small pencil + trash on
hover per row (consistent with the statuses/board editors). Completed/Discarded rows show
neither (or Edit-view-only). The Plan tab keeps Start/Complete as today.

---

## 5. Backfill (fixes problem B for existing data)

Existing Completed sprints have no `snapshot`. We reconstruct one from the activity log.
The reconstruction logic already exists client-side: `_parseSprintActivities(itemId)`
(`roadmap.html:21950`) + `itemSprintHistory` replay the `"Sprint planning:"` messages.

Two ways to run it - **this is open question A**:

- **Option A1 (server-side, my recommendation):** a boot-time migration in `server.py`
  (`init_team_db` -> a new `_migrate_sprint_snapshots`, idempotent, `print()` not audited)
  replays the **full** `activities` table (not the 500-row client window) to build each
  completed sprint's snapshot once and persist it into the `sprints` config. Pro: correct
  across all history, runs once, no user-visible cost. Con: re-implements the JS parser in
  Python (a second copy of a fragile parser).
- **Option A2 (client-side, one-time):** on first load after deploy, reuse the existing JS
  parser to build snapshots for completed sprints lacking one, then `saveSprints`. Pro: one
  parser, reuses tested JS. Con: bounded by the 500-row `/api/activities` window - older
  sprints may reconstruct incompletely; depends on an admin loading the Sprints tab.

Backfilled snapshots carry a flag (e.g. `snapshot.reconstructed=true`) so the UI can note
these figures are reconstructed, not captured live.

---

## 6. Server changes (`server.py`)

- `_SPRINT_STATES`: add `"Discarded"` (line 3439).
- `PUT /api/sprints` (`put_sprints`, ~3453): keep id+name + single-Active validation;
  accept the new state; accept the additive `snapshot` field passthrough (it's just stored
  in the config blob - no schema enforcement needed beyond size sanity).
- Audit depth - **open question B**:
  - **B1 (keep coarse):** leave the existing `write_audit(team,"beta:sprints",...,{"count":N})`.
  - **B2 (my recommendation):** let `PUT /api/sprints` accept an optional
    `{reason, detail}` and write a more descriptive audit line (edit / discard / backfill),
    so an Active-date edit or a discard is attributable and prominent. Boot backfill logs
    under a system actor (or just `print()`s, since migrations aren't audited).
- If A1: add `_migrate_sprint_snapshots(team)` to the `init_team_db` migration chain.

## 7. Client changes (`roadmap.html`, beta module)

- `sprintStats` (20161): snapshot-first, live-fallback.
- `doCompleteSprint` (20126): build + attach `sp.snapshot` before finalize.
- `renderSprintsTab` (20173): per-row Edit/Discard controls; render `Discarded` rows;
  show a "reconstructed" hint on backfilled snapshots.
- New `editSprint(id)` and `discardSprint(id)` functions (+ confirm modals).
- `SPRINT_BADGE`: add `Discarded`.
- If A2: one-time backfill on Sprints-tab first render.
- Bump `APP_VERSION`.

## 8. Verification

- `pytest` green; extend `tests/` for: new `Discarded` state accepted by `PUT /api/sprints`;
  snapshot field round-trips; (if B2) audit reason passthrough. If A1, a small test that the
  migration is idempotent and builds a snapshot from seeded activities.
- JS syntax check on **both** `<script>` blocks; `ast.parse` on `server.py`.
- Browser E2E on a throwaway team: complete a sprint that carries an item over -> Sprints
  tab shows the true committed/completed/carry-over (not 0/0); edit a Planned sprint's dates;
  discard a Planned sprint -> its items are removed from the sprint (sprintId cleared),
  status unchanged, and a history entry is logged; confirm Active sprints offer no Discard.
- Deploy: scp both files + `sudo systemctl restart roadmap`; confirm `/api/version`.

---

## 9. Review findings carried in (from the earlier read-through)

- No hard architectural contradiction: sprints are a config blob, snapshot fields are
  additive, and there is a single completion path to hook.
- #2 `Discarded` requires the `_SPRINT_STATES` edit (folded into section 3.2/6).
- #3 the completion flow is client-side and **non-transactional** (per-item PUT chain with a
  `.catch` that still finalizes). The snapshot is built from the in-memory outcome map, so a
  partial-failure completion can snapshot an item as "completed" whose PUT actually failed.
  **Accepted for this stage** (matches today's behavior); a server-side atomic completion
  endpoint is on the backlog.
- #5 snapshot stores per-item key/name/points/outcome, not bare ids (folded into 3.1).
- Minor: date ranges render with the app convention `" → "` (not an em/en dash);
  `nextSprintName()` counts discarded sprints when numbering the next one - harmless, noting it.

---

## Locked decisions

1. **Backfill location:** **A1 - server-side full-table replay** (`_migrate_sprint_snapshots`
   in `init_team_db`, idempotent).
2. **Audit depth:** **B2 - enhanced.** `PUT /api/sprints` accepts an optional `{reason,
   detail}`; edit/discard log descriptively (Active-date edits prominently). Boot backfill
   `print()`s (migrations aren't audited).
3. **Edit scope:** **Planned + Active only.** Completed sprint dates are historical.
4. **Discard an Active sprint:** **disallowed.** Complete is the only exit for Active.
   Discard applies to Planned sprints only.
5. **1c item handling (spec as written):** discard **clears `sprintId` and logs to item
   history, no status change** - do **not** set Deferred.
6. **Completion-flow non-atomicity:** accepted for this stage; server-side atomic completion
   endpoint goes on the backlog.
