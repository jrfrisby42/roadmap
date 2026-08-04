# Shared Devs (owner) filter persists across ALL views

Status: PLANNED. Frontend only (`roadmap.html`, beta shell). No server change. Version bump is
J.R.'s. Decided 2026-08-04 (J.R. chose "drive Calendar too").

## Problem / why it happens today
The shared filter chips carry only among `_FRZ_SHARED_VIEWS = {gantt, kanban, list}`. Navigating
to another view via the rail uses `_frzCarryQS(false)` (project only), so the chip params drop
and `applyURLToFilters` resets the panels. Dashboard/Planning/Reports each ALREADY filter their
data by the owner selection (`getDashboardItems` / `getPlanningItems` / Reports `_rptComputed`
read `getSelectedDevs()`), so they just need to RECEIVE the carried selection. Calendar is the
exception - it filters via its own `state.calFilter` (owner/user/type/cap) and does not read the
shared owner at all.

## Key insight (keeps this low-risk)
`owner` is in the CFG.chips of every view EXCEPT calendar (gantt/kanban/list/planning/dashboard/
reports). So once (a) navigation carries the chips and (b) calendar exposes+honors owner, the
Devs selection self-serializes (syncURL) and re-applies (applyURLToFilters) on every view - with
NO change to the fragile ride-along / resurrect-guard code.

## The change
1. **Rail view-nav carries chips everywhere.** The delegated `view:` handler uses
   `_frzCarryQS(!!_FRZ_SHARED_VIEWS[_v])`; change to `_frzCarryQS(true)`. Chips now ride to every
   view. (No regression for pass-through of un-exposed chips: a view that doesn't expose a chip
   still drops it on its own syncURL, exactly as today.)
2. **Calendar exposes owner.** `CFG['team-calendar'].chips` gains `'owner'`. Calendar renders its
   OWN filter row (it does not call `renderBar`), so this adds no visual chip - it only makes
   `applyURLToFilters` apply the carried owner (panel kept, not cleared) and `syncURL` emit it, so
   the Devs selection persists onto the calendar.
3. **Calendar honors the shared Devs selection.** `_calData` intersects its owner list with
   `getSelectedDevs()` (null = all); `_calUnassignedData` returns null when a shared Devs filter is
   active (unowned rows can't match an owner filter). The calendar keeps its own finer owner
   picker (composes as an AND).
4. **Filter-bar height parity.** Remove the calendar-only `.frz-cal-active .frz-filterbar
   { padding:6px 14px }` trim so every view's bar is `padding:10px 14px` (same height).

## Scope
- Focus is the **Devs (owner)** filter (the ask). Other chips persist across views that expose
  them (gantt/kanban/list/reports have all; planning has owner/status; dashboard owner; calendar
  owner). Full chip parity everywhere is NOT in scope (would need the ride-along change).
- No change to Saved Views storage, the measures, or the server.

## Verify (local seed + Chrome)
Set Devs = one owner on Gantt; navigate to Kanban, List, Planning, Dashboard, Reports, Calendar -
each shows only that owner's items/rows and the selection sticks (URL keeps `owner=`). Clear
resets everywhere. Calendar bar height matches the others. Light + dark.
