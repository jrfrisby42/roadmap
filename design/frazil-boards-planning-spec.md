# Frazil Roadmap `/beta` — Boards, Sprint Planning & Releases Spec

Builds on the existing /beta shell (frazil-rail-spec.md). Same tokens, same
chip/popover components, same ground rules: additive to production, reuse
existing data and views, status/priority badge colors untouched.

This spec has three features. Build and ship them IN ORDER — each is
independently useful and testable:
  Feature A: Custom Kanban Boards
  Feature B: Planning view rework + Sprint entity + readiness modal
  Feature C: Sprints & Releases index tabs

---

## Feature A — Custom Kanban Boards

### Concept
A Board is a named, shared column configuration for the Kanban view. Each
board defines an ordered list of columns; each column maps to ONE OR MORE
statuses and has a designated "default drop status."

### Data model
```
Board {
  id, name,                  // e.g. "Wasatch Board"
  columns: [
    { name,                  // e.g. "Done"
      statuses: [status…],   // e.g. ["Released","Done","Inactive"]
      dropStatus }           // which status a dragged-in card receives;
                             // must be one of `statuses`
  ],
  position                   // sort order in the rail
}
```
Boards are GLOBAL/shared (visible to all users). Creating/editing/deleting
boards is available to all users for now (revisit permissions later). A
status may appear in at most one column per board; statuses not assigned to
any column are hidden on that board.

### Rail
New section `BOARDS` between PROJECTS and SAVED FILTERS:
- First entry is the built-in "All statuses" board (current Kanban
  behavior; not editable, not deletable).
- Then user-created boards, with a kanban icon, sorted by position.
- Heading "+" opens the board editor (create). Hover "…" on a board:
  Edit / Duplicate / Delete (confirm dialog on delete).
- Clicking a board navigates to /beta/kanban?board={id}. The active board
  highlights in the rail; the Kanban top-bar title shows the board name
  (e.g. "Kanban · Wasatch Board").
- In the collapsed rail, the BOARDS section hides (same as Projects/Saved).

### Board editor
A modal (reuse the popover/modal styling from Save filter, scaled up):
- Name field.
- Column list: add / rename / delete / drag-to-reorder columns.
- Per column: multi-select of statuses (each status selectable in only one
  column — show statuses already used elsewhere as disabled with a hint),
  and a default-drop-status select limited to the column's statuses
  (auto-set when the column has exactly one status).
- An "Unassigned statuses" line showing which statuses will be hidden.
- Save / Cancel. Validation: name required, ≥1 column, every column ≥1
  status.

### Kanban behavior with a board active
- Columns render in board order with board names; a column shows the
  combined count of its statuses; cards within a multi-status column may
  optionally show their specific status as a small badge when the column
  has >1 status.
- Dragging a card into a column sets the column's dropStatus and logs the
  change to the item's activity feed.
- Filter row, "Hide empty columns," and saved filters work unchanged on
  top of any board. Saved filters capture the board param too (it's just
  part of the URL).

---

## Feature B — Planning rework: Sprints + readiness flow

### Terminology
The time-box entity is a SPRINT. Remove all "Session" / "New Session" /
"planning session" wording from the UI. The history of old sessions can be
left accessible under the Sprints tab (Feature C) as legacy entries or
migrated — ask the user which before migrating data.

### Sprint data model
```
Sprint {
  id, name,                  // default auto-name "Sprint {n}"; editable
  goal,                      // optional short text
  startDate, endDate,        // endDate defaults to start + 2 weeks
  state,                     // Planned | Active | Completed
  scope: "global"            // single global cadence for now; field exists
                             // so per-pod scoping can come later
}
```
Rules: at most ONE Active sprint at a time (global scope). Items link to
sprints via the existing Sprint field (now a reference to Sprint id).

### Plannable types
Config constant PLANNABLE_TYPES = [Feature, Enhancement, Bug Fix, Task].
Maintenance/recurring items and child items (items with a parent) never
appear in the backlog pane or sprint pane. Make the list a single editable
constant so it's easy to change.

### Planning view structure
/beta/planning becomes a tabbed view. Tab bar sits at the top of the
content area (below the filter row), styled like the mockup's segmented
control but smaller: **Plan | Sprints | Releases**. Routes:
/beta/planning (Plan), /beta/planning/sprints, /beta/planning/releases.

### Plan tab — two-pane layout
LEFT (≈55%): **Backlog** — the existing priority stack, filtered to
PLANNABLE_TYPES, excluding items already in a non-completed sprint.
Existing drag-to-reorder priority behavior is kept. Each row gains an
"Add to sprint →" affordance (button on hover, plus drag-into-right-pane).

RIGHT (≈45%): **Sprint pane**
- If no Planned/Active sprint exists: empty state with "+ Plan a sprint"
  → small modal: name (pre-filled), start date, end date (auto +2wk),
  optional goal → creates a Planned sprint.
- Shows the sprint header: name, dates, goal, state badge, and a capacity
  line: total story points and total time estimate of contained items
  (display only in v1 — no capacity limit enforcement).
- Contains the sprint's item cards (compact rows: key, name, points,
  estimate, owner, status badge).
- Actions: "Start sprint" (Planned→Active; requires startDate ≤ today’s
  date is NOT required — allow future starts; requires ≥1 item),
  "Complete sprint" (Active→Completed; see completion flow), remove item
  (returns to backlog, clears its Sprint field).

### "Add to sprint" readiness flow — ONE modal, not chained prompts
When an item is added (button or drag), evaluate readiness. If the item
needs nothing, add it silently. Otherwise open a single **"Make sprint-
ready"** modal listing ONLY the relevant rows:

1. START DATE — date input shown if the item's start date is empty OR
   earlier than the sprint start; defaulted to the sprint start date.
2. ESTIMATE — input shown only if Time Estimate is empty.
3. STORY POINTS — input shown only if Story Points is empty.
4. STATUS (informational, no input) — if current status is below Approved
   (e.g. New, Inactive, Needs Scoping): "Status will change: New →
   Approved". Statuses at or above Approved are never touched.
5. SPRINT (informational) — "Sprint will be set to {sprint name}".

One primary button "Add to sprint" applies everything atomically; Cancel
applies nothing. Every applied change is written to the item's
Activity & History feed (one entry per field, attributed to the user, e.g.
"Sprint planning: Status New → Approved"). Required inputs (estimate,
points) must be filled to confirm — no skipping in v1.

### Sprint completion flow
"Complete sprint" opens a modal: "{n} of {m} items are not Done/Released."
Radio choice for the unfinished items: "Move to next sprint" (creates the
next Planned sprint if none exists, pre-filled to start the day after this
sprint ends) or "Return to backlog." Record the carry-over count on the
completed sprint (for the Sprints index). Items completed keep their
sprint link permanently.

---

## Feature C — Sprints & Releases tabs

### Sprints tab (/beta/planning/sprints)
A table, newest first: SPRINT (name, goal as subtitle) · DATES · STATE
badge (Planned/Active/Completed — use neutral/accent/green tints, NOT the
item-status palette) · ITEMS (done/total) · POINTS (completed/committed) ·
CARRY-OVER (count rolled out at completion). Active sprint row gets a
subtle accent-soft background. Clicking a row expands inline (or routes to
/beta/planning/sprints/{id}) showing the sprint's items as compact rows
and the same stats. No charts in v1.

### Releases tab (/beta/planning/releases)
Introduces a lightweight Release entity:
```
Release { id, name, targetDate, state: Unreleased | Released,
          releasedDate? }
```
Items gain a Release field (single reference, optional; shown on the item
page Details and as an optional List column, default hidden).

The tab shows release cards/rows: NAME · TARGET DATE · STATE · progress
bar (items in a terminal status / total linked items, with the fraction as
text — never a bare bar) · "+ New release" button. Row actions: edit,
"Mark released" (sets state + releasedDate; if unfinished items are
linked, warn and list them). Clicking a release shows its linked items.
Items are linked to a release from the item page (Release field) and via
multi-select on the List view if trivially easy; otherwise item page only
for v1.

### Counts
The rail's Planning count pill changes to mean "items in the current
Planned/Active sprint" (was: priority stack size). If no sprint exists,
hide the pill.

---

## Shared requirements

- All new routes live under /beta; production untouched; everything
  scoped under the existing beta style scope.
- All new controls use the established components: chips, popover (from
  Save filter), modal styling, accent #0059A9 — zero indigo.
- Item status/priority badge colors remain untouched everywhere.
- Every automated field change (drop-status on boards, readiness modal,
  sprint completion) writes to the item's Activity & History.
- URLs are the state: board ids, planning tabs, sprint/release ids all
  routable and shareable.

## Acceptance criteria

A1. Boards section appears between Projects and Saved Filters; "All
    statuses" board is first, uneditable, and matches old Kanban exactly.
A2. Create a board with a multi-status "Done" column; Kanban shows merged
    counts; dragging a card in sets the column's dropStatus and logs it.
A3. A status can't be assigned to two columns; unassigned statuses are
    hidden on that board and listed in the editor.
A4. Board URL (?board=id) is shareable; saved filters capture it.
B1. No "Session" wording remains anywhere in /beta.
B2. Plan tab: backlog excludes Maintenance, child items, and items in a
    non-completed sprint.
B3. Adding a fully-ready item to a sprint shows no modal; adding an item
    missing estimate+points with status New shows ONE modal with exactly
    those two inputs plus the status and sprint info lines; confirm
    applies all changes atomically and logs each to item history.
B4. Only one Active sprint can exist; Start requires ≥1 item.
B5. Completing a sprint with unfinished items forces the roll-over/backlog
    choice and records carry-over count.
C1. Sprints tab lists sprints with items, points, carry-over; active row
    highlighted; sprint state badges do NOT reuse item-status colors.
C2. Releases tab: create a release, link 3 items from the item page,
    progress shows 0/3 → mark one Released-status item and progress
    updates; "Mark released" with unfinished items warns and lists them.
C3. All new routes deep-link correctly when pasted into a fresh tab.

## Out of scope (v1)
WIP limits and per-column limits on boards; per-pod sprint scoping;
capacity enforcement and burndown/velocity charts; release notes
generation; Jira Fix Version sync; board-level swimlanes; permissions on
board/sprint/release editing.

## Decisions already made (do not re-ask)
Boards are shared, columns are multi-status with a default drop status,
sprints are global with a single Active at a time, the readiness flow is
one combined modal, "Sprint" replaces "Session," plannable types exclude
Maintenance and children, Planning is tabbed (not three rail views), and
Releases are explicit named entities with a progress bar.
