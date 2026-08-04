# Reports: page-level layout + shared filters (Phase 1)

Status: PLANNED (Phase 1). Frontend only (`roadmap.html`, beta Reports module). No server change.
Additive to the 5.12.0 measures+pivot work (all measures/aggs/pivot/CSV logic is preserved).
Version bump is J.R.'s.

Fixes the three reported issues in one restructure: (1) Reports ignores Saved Filters, (2) the
content reads as a boxed "widget" not a page, (3) the "Work Items" card title is cramped. The
root cause is shared: Reports is built as a self-contained card with its OWN `_rptFilters`,
disconnected from the shared top-bar filter chips + Saved Views that every other view uses.

Decided direction (2026-08-04): make Reports behave like the Dashboard - shared filter chips
across the top (so Saved Views scope it), report content as page-level sections (no widget
card). Report picker (multiple named reports) is a later Phase 2, NOT this slice.

---

## What Phase 1 changes (grounded in code)

1. **Adopt the shared filter model.**
   - `CFG.reports` (roadmap.html ~L18292) becomes `{ chips:['owner','assignee','type','status'],
     extra:['priority','dept'], opts:[] }` (mirrors `list`/`kanban`), so `renderBar()` draws the
     SAME chip bar and Saved Views apply.
   - The Reports view-enter (`showReports`, the block ~L19616 that currently does
     `frzFilterBar.innerHTML=''  // Reports owns its own filter row`) instead calls `renderBar()`.
   - `_rptComputed`'s filter block (~L19683) stops reading its own `f.status/type/owner/priority/
     dept/assignee` and instead reads the shared getters, copying `_kanbanFilteredItems`'s proven
     conditions: `getSelectedDevs/Products/Types/Statuses/Assignees` + `priorityMatches` +
     `departmentMatches` + `itemMatchesSearch` (the top-bar search) + the rail `state.project`.
   - The beta `rerender()` (~L19367) gains a Reports branch (`if(state.view==='reports'){
     renderReports(); return; }`) so a chip/search/saved-view change repaints the report (today
     `renderCurrentView` has no Reports case, so chip changes would not refresh it).

2. **Report-local controls stay report-local** (no top-bar equivalent): the **Date basis**
   (Requested/Completed), the **date range** (from/to), and the **Group by** pivot selector live
   in a slim control strip at the top of the report area, next to Export + the item count. These
   remain in `_rptFilters` (`basis`, `from`, `to`, `group`); the shared-chip keys are removed from
   `_rptFilters`.

3. **Page-level layout (drop the widget card).** `renderReports` stops wrapping everything in a
   single `.frz-rpt-sec` card with a "Work Items" header. The control strip, KPI tiles, aggregate
   cards, aging, and the table/pivot render as page-level sections directly in `#reportsBody`
   (like the Dashboard's sections), so it fills the view area and there is no redundant title.

## Explicitly OUT of Phase 1
- Report picker / multiple named reports (Work Items / Aging / SLA / velocity) - Phase 2.
- Adding Date-basis / Group-by to the Saved View model (they stay per-session for now).
- Any server change; any change to the measures/aggs/pivot/CSV computations.

## Touch-points
- `CFG.reports` (chips/extra).
- `showReports` view-enter: `renderBar()` instead of clearing the bar.
- `rerender()`: Reports dispatch branch.
- `_rptComputed`: shared-getter filter block; `_rptFilters` trimmed to basis/from/to/group.
- `renderReports`: report-local control strip (basis/range/group/export/count) + page-level
  sections; remove the shared-filter selects + the `.frz-rpt-sec` wrapper + its rewiring.
- CSS: the KPI/agg/pivot rules are reused; adjust the removed-card spacing so sections sit at
  page level. No indigo, no `!important`, no em dashes.

## Verification (local seed + Chrome)
Confirm: top-bar chips (owner/assignee/type/status/priority/dept) + search + a Saved View all
scope the report; the rail project scope still applies; Date basis / range / Group by still work
from the control strip; the page fills the view area (no boxed widget, no "Work Items" title);
CSV still matches; light + dark.
