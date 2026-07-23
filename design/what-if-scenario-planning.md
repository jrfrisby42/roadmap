# What-if / scenario planning (design map)

**Status:** design-only, no code. A map for the highest-differentiation feature on the
roadmap: let someone make hypothetical changes - drag a release date, add items to a
sprint, mark a person out, shift a start date - and see **live** who tips over and what
slips, **without committing anything**. Then commit or discard.

## Why it differentiates

Issue trackers (Jira/Linear/Asana) have no capacity model, so they can't answer "if I
add this, who breaks?" Resourcing tools (Float/Resource Guru) model capacity but aren't
fused with sprints/issues/releases. Flow already has both - a capacity engine AND
commitment tracking - so it can answer the planning question those tools can't. Making
that engine *interactive and non-committal* is the wedge.

## The core insight (why the engine is ~80% there)

Every capacity/commitment/conflict surface in Flow reads from **five module-level inputs**
in the production script and derives everything else from them:

| Input (server.py-loaded, block-0 `let`) | Location |
|---|---|
| `projects` (all items) | `roadmap.html:2306` |
| `ownerCapacity` (per-owner base) | `:2311` |
| `_capOverrides` (per owner+day override) | `:6060` |
| `_assignments` (PTO/training/etc.) | `:6105` |
| `_assignmentImpacts` (derived; rebuilt by `buildAssignmentImpactMap`) | `:6106 / :6109` |

The read/compute functions all close over those vars:
`getEffectiveCapacity` (`:6123`), `itemCapacityOnDay` (`:3208`), `buildCapacityUsageMap`
(`:2898`) → `buildCapacityGraphHtml`, `capacityHealthClass` (`:6131`),
`computeConflictingIds` (`:3238`), the dashboard heatmap (`:~13640`), and the Stage-4
`_sprintPodLoad` (beta). **No new math is needed.** A scenario is just a **cloned,
mutated copy of those five inputs**, with the engine re-run against the copy. Exit =
restore the originals.

## Architecture: the scenario substrate

Implement in **block 0** (that's where the five inputs live and can be reassigned; the
beta shell drives the UI and calls in via `_call`).

- `enterScenario()`: deep-clone `{projects, ownerCapacity, _capOverrides, _assignments, _assignmentImpacts}` into `_scenarioBackup`; set `_scenarioActive = true`; render. From now, mutations apply to the live vars (which are now the scenario copy).
- `exitScenario()` / discard: restore all five from `_scenarioBackup`, clear the flag, re-render, drop the overlay.
- `commitScenario()`: replay the recorded mutations through the **existing** endpoints (item `putItemGuarded`, capacity-override, assignments, sprint/release PUTs), batched, with a preview of exactly what will be written; then exit. OCC (the existing `_baseUpdatedTs` 409) handles anyone who edited underneath you.
- **Mutation log:** record each hypothetical change as a structured op (`{kind, target, from, to}`) so we can (a) render the diff/summary, (b) replay on commit, (c) support undo.

### The one non-negotiable: a hard read-only guarantee
While `_scenarioActive`, **no write may reach the server.** The single chokepoint is the
`API` object (`:2219`): guard `API.put`/`API.post`/`API.delete` so that for item /
capacity / assignment / sprint / release paths they **mutate the in-memory overlay
instead of calling `fetch`** (and everything else - Jira sync, autosave, recur - is
blocked with a toast). A leak here means accidental real edits, so this guard is the
feature's safety spine and gets its own tests. A persistent banner ("Scenario mode -
nothing is saved. [Commit] [Discard]") must be visible on every surface while active.

## The levers (what you can change) and readouts (what recomputes)

| Lever (hypothetical change) | Overlay mutation | Recomputes (for free) |
|---|---|---|
| **Mark a person out** (PTO/leave) | add an `_assignments` row → `buildAssignmentImpactMap()` | effective capacity ↓ → heatmap, cap calendar, Gantt capacity graph, sprint gauge, conflicts |
| **Add / remove items in a sprint** | set/clear `p.sprintId` on overlay items | Stage-4 `_sprintPodLoad` pod commitment vs capacity; overcommit warnings |
| **Change an item's owner / assignee** | set `p.dev` / `p.assignee` | per-owner load, conflicts, who-tips-over |
| **Shift a start date / estimate / parallelResources** | set `p.start` / `p.dueWeeks` / `p.parallelResources` | `itemCapacityOnDay` → load, conflicts, the item's own due/expected |
| **Move a release date** | set `release.date` (overlay) | which linked items now finish after the target = "what slips" |

Readout surfaces (in priority order): the **Stage-4 sprint gauge** (already aggregates),
the **capacity heatmap / cap calendar** (already per owner+week/day), **conflicts**
(advisory), the **Gantt** (bars move), and a **scenario summary panel** (the diff).

## Cascade / scheduling decision (important)
Moving a start date does not, by itself, reschedule dependents. Flow has a limited
start-date cascade for non-parallel `requires`, but a full dependency-graph reflow is
its own project. **Decision for v1:** model **direct** effects (the item you changed +
capacity/conflict recompute) and **list** the dependents that *would* be affected
("3 items depend on this and would shift"), rather than silently auto-rescheduling the
graph. Auto-reflow is a later phase. Same for releases: v1 compares each linked item's
projected finish (due/revised/expected) against the (hypothetical) release date and flags
the ones that miss - it does not re-plan the items to hit the date.

## UX
- **Enter:** a "What-if" toggle (rail or the Gantt/Planning toolbar). Entering snapshots
  state and shows the banner.
- **Make changes:** the same controls you already use (drag a Gantt bar, the sprint
  add/remove, an inline owner/date edit, a "mark out" action on the calendar) - but in
  scenario mode they hit the overlay, not the server, and everything recomputes instantly.
- **See impact:** a **scenario summary panel** - "Changes: moved Release 3 to Aug 14;
  added FRAZ-2 to Sprint 5; Weto PTO Aug 4-8. Effects: Wasatch over by 1.5w in Sprint 5;
  2 new conflicts; Release 3 - 3 items miss the new date." Before/after deltas on the
  gauges (e.g. Wasatch 3.2w → 4.1w / 4.0w).
- **Resolve:** Commit (preview → batched real writes) or Discard (restore).

## Phasing (each shippable)

- **Phase 0 - substrate.** `enterScenario/exitScenario/discard` + the deep-clone overlay
  + the **`API` read-only guard** + the banner. No levers yet beyond enter/exit. This is
  the foundation and the safety spine; ship + test it alone (a test proving no `fetch`
  write escapes in scenario mode).
- **Phase 1 - the two highest-value levers on Planning.** "Mark a person out" (add a
  hypothetical assignment) and "add/remove sprint items," with the Stage-4 gauge + a
  compact capacity readout recomputing live, and a minimal summary panel. Commit/discard.
  This alone delivers "who tips over if I load this sprint / lose this person."
- **Phase 2 - dates & the Gantt.** Drag start/due and move a release date in scenario
  mode; bars move, heatmap/conflicts recompute; the "what slips" list. Full before/after
  diff panel.
- **Phase 3 - named scenarios.** Save/name scenarios (per-user, localStorage like saved
  views), reopen, and **compare two** side by side. Optional server-side sharing later.

## Persistence & multiplayer
v1 is **ephemeral and local** to the user's browser - nothing is saved or shared, which
neatly sidesteps collaboration/locking. A committed scenario simply becomes normal edits
(subject to the existing OCC). Named/saved scenarios (Phase 3) live in `localStorage`
per user, mirroring Saved Views; server-side shared scenarios are a later, deliberate step.

## Risks / decisions to settle before building
1. **Read-only guard scope** - confirm the full list of write paths to intercept (`API`
   put/post/delete for items, capacity overrides, assignments, sprints, releases; plus
   autosave, Jira sync, recurrence spawn). Miss one = a real edit leaks. (Highest risk.)
2. **Cascade depth** - v1 "direct effects + list affected dependents" vs. full reflow
   (recommend the former; confirm).
3. **Release what-if semantics** - "flag items that miss the new date" (v1) vs. "re-plan
   to hit it" (later). Confirm the simpler reading is enough for v1.
4. **Which surfaces reflect scenario in v1** - recommend Planning gauge + heatmap first;
   Gantt dragging in Phase 2.
5. **Commit conflict handling** - rely on the existing per-item OCC 409 on replay; decide
   the UX when a committed scenario partially conflicts (apply the rest + report).
6. **Performance** - deep-cloning `projects` + re-rendering on every drag is fine at
   current scale; revisit once teams are large (ties to the `/api/all` bulk-load ceiling).

## Bottom line
The build is mostly **plumbing, not modelling**: a clone-mutate-restore overlay on five
existing inputs, a hard read-only guard on the `API` chokepoint, and UI to drive the
levers + show the diff. The capacity/commitment math is already done and battle-tested
(Stage 4/4.1). Start with **Phase 0 (substrate + guard)** and **Phase 1 (mark-out +
sprint-load on Planning)** - that slice alone is the differentiating demo.
