# Reports: "Measures + Pivot" slice (plan)

Status: PLANNED, not started. Frontend only (`roadmap.html`, beta Reports module). No
server change. Additive. Preserves the existing team-aware behavior. Version bump is J.R.'s.

Decided direction (2026-07-31): "Measures + pivot" first; output shaped as "in-app view +
richer CSV/export". Later slices (trend, SLA/aging, sprint/velocity, scheduled email) are
queued below, not in this slice.

---

## Where Reports stands today (grounded in code)

One "Work Items" section in `renderReports()` (`roadmap.html` ~L19586):
filters -> count-only aggregate cards -> a flat item table -> CSV export.

- Row model: `_rptComputed()` (~L19467) builds one row per item with, among others,
  `key, status, product(Project), owner(pod dev), requested(createdAt), requestedBy(reporter),
  start, due, revised, completion(completedAt), type, priority, dept, assignee, description,
  notes, resolution, jira, attachCount, overdue, open` PLUS three already-computed measures:
  `age`, `resolutionTime` (createdAt -> completedAt), `responseTime` (createdAt ->
  firstResponseAt). Measures are `null` when the source timestamp is missing (rendered
  "n/a", never 0).
- Columns: `_RPT_COLS` (~L19530), team-aware via `_rptHas`/`_rptVisibleCols` (a column shows
  only when the team populates it; `always:true` columns pin).
- Aggregates: `_rptCount` + `_rptAggHtml` produce COUNT-by cards (by project/owner/technician/
  status/type/priority/dept + open/overdue). Team-aware.
- Filters: `_rptFilters` (~L19459) = from/to (on `createdAt` only), status, type, project
  (mirrors rail scope `state.project`), owner, priority, dept, assignee.
- CSV: `_rptExportCsv` (~L19566) exports the visible columns of the filtered item rows.

Key gap: it already COMPUTES the valuable measures (age/resolution/response/overdue) but only
ever shows COUNTS and raw columns. It never aggregates a measure, and it is a snapshot with no
trend. Same gap for Dev and for IT/Ops.

---

## This slice - what we add

### A. KPI summary tiles
A new tile row (above the aggregate cards) computed over the FILTERED `rows`:

- Open, Overdue, Completed (in window), Median resolution (d), Avg resolution (d),
  Avg first-response (d), Throughput (completed in window).
- New helpers `median(nums)` / `avg(nums)` over non-null values only.
- Team-aware: a tile hides when its source is unpopulated across the set (e.g. no
  `firstResponseAt` anywhere -> no response tile), mirroring `_rptHas`. In-scope-but-empty -> "n/a".

### B. Date-basis toggle
New control by the range: "Date basis: Requested | Completed" (default Requested = today).
Stored as `_rptFilters.basis`. When "Completed", the from/to range filters on `completedAt`
instead of `createdAt` and the label flips to "Completed from/to". Unlocks throughput queries
("what did we close in July") that are impossible today. One branch in `_rptComputed`'s date filter.

### C. Group-by pivot
New "Group by" select: None | Owner | Assignee | Department | Status | Type | Project | Priority.

- None = today's flat item table (unchanged).
- Grouped = the item table is REPLACED by a pivot: one row per group with metric columns -
  Count, Open, Overdue, % Overdue, Avg age (d), Median resolution (d), Avg response (d) - plus
  a bold TOTAL row. Team-aware columns (drop resolution/response if unpopulated).
- Rationale for REPLACE (vs nested collapsible item rows under group headers): cleaner, less
  code, and the flat item list stays one click away at "None". OPEN DESIGN CALL - J.R. may
  prefer item rows visible under each group header with subtotals; that is more code but doable.

This is the per-pod / per-department / per-technician performance view - serves Dev (load by
owner/assignee) and IT/Ops (queue health by department/type) from one control.

### D. Richer CSV
- Flat mode -> per-item CSV, unchanged.
- Grouped mode -> exports the pivot rows + TOTAL row (the per-group/summary CSV), following the
  same visible metric columns. Useful pasted into a deck or sheet.

---

## Code touch-points (all in the beta Reports module)

- `_rptFilters` - add `basis:'requested'`, `group:''`.
- `_rptComputed` - date filter honors `basis`; no other row change (measures already exist).
- New: `median()`, `avg()`, `_rptKpis(rows)`, `_rptPivot(rows, groupKey)` (returns group rows +
  totals), `_rptPivotColsVisible()` (team-aware metric columns).
- `renderReports` - render the KPI tile row; render the basis + group-by controls; when grouped,
  render the pivot table instead of the item table; wire the two new controls (re-render on change).
- `_rptExportCsv` - branch on `group`: flat -> items (today); grouped -> pivot + TOTAL.
- CSS: a `.frz-rpt-kpis` tile row + a `.frz-rpt-pivot` table, scoped under `.frz-beta`, brand
  styling, no `!important`, no indigo.

No server change. No new endpoint. No em dashes in UI copy.

---

## Verification (local seed + Chrome, per house harness)

Seed a team with a mix of open/overdue/completed items across owners + departments, some with
`completedAt`/`firstResponseAt`. Confirm: KPI tiles compute median/avg correctly and hide when a
source is empty; date-basis Completed changes the set and the "Completed in window" throughput;
group-by pivot subtotals + TOTAL reconcile with the flat counts; CSV in each mode matches the
on-screen table.

## Effort

~1 - 2 days for a solid v1 (data already computed; the work is aggregation helpers + the pivot
render + two controls + CSV branch + styling).

---

## Later slices (queued, not this one)

1. Trend over time: created vs completed per week/month + rolling open backlog (the "are we
   keeping up" chart). Needs week/month bucketing; small inline bar/line via the dataviz approach.
2. IT/Ops SLA & aging: aging buckets of OPEN items (0-2d / 3-7d / 8-30d / 30d+) + SLA target by
   priority/type with breach flags (new per-team config for targets).
3. Dev sprint/velocity report: per-sprint committed vs completed points, throughput, carryover,
   completion %. Data already durable in `sp.snapshot` (points + outcomes) - mostly presentation.
4. Saved report presets + scheduled weekly email/CSV (server work via existing SES).
5. Reopened-rate / status-regression churn (needs history parsing).
