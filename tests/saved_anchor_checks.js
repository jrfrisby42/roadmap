/* SAVED-ANCHOR-2 regression check - copy/paste browser-console procedure (behavioural layer).
 *
 * Fault (SAVED-ANCHOR-1): a saved filter naming a config value later removed (IT's status "Planned")
 * applied but never anchored - applyPanel pruned the dead value from the live URL, the matcher compared
 * that pruned URL to the UNPRUNED saved record, never equalled, and _frzActiveSaved nulled the anchor.
 * Permanently un-anchorable. The fix prunes the SAVED side to the same panel source before comparing.
 *
 * The matcher/state are closure-scoped (not console-readable), so these checks assert the RENDERED
 * outcome. Region agreement + the x-clear reuse tests/saved_paint_checks.js (savedRegionsAgree,
 * savedClearClearsAfterSwap) - load that too. This file documents the SAVED-ANCHOR-2-specific cases
 * and how to build the removed-value fixture without touching IT.
 *
 * HOW TO BUILD THE FIXTURE (development, no IT access needed):
 *   1. On a chip-bar view (e.g. /list), set Status to two or more values and Save filter ("Stale Test").
 *   2. Admin -> Statuses: delete one status the filter used (only deletable if 0 items hold it; add a
 *      throwaway status first, save the filter on it, then delete that status). This makes the saved
 *      record name a value no longer in config - exactly IT's shape.
 *   3. Reload, apply "Stale Test" from the rail.
 *
 * CASE 1 (headline): after applying "Stale Test", run savedRegionsAgree() (saved_paint_checks.js).
 *   pass=true means the anchor SURVIVED the removed value: rail row aria-current + fdot, chip named +
 *   is-active, crumb named. On the pre-fix build this returns pass=false (all rested) - the revert demo.
 *
 * CASE 2 (x clears): collapse the rail, then savedClearClearsAfterSwap() - the x on the now-anchored
 *   chip actually clears the filter (params gone, chip rests). An anchored-but-unclearable chip is the
 *   POLISH-5 shape; this proves it is clearable.
 *
 * CASE 4 (no loosening - MUST still fail): apply a filter with two VALID statuses, then hand-remove one
 *   status chip value in the bar so the live URL carries a subset. Run savedRegionsAgree(): the filter
 *   must NOT read active (it is no longer that filter). If it anchors, the prune was written too loosely.
 *
 * CASE 9 (rest still rests): apply the filter, switch to Dashboard (no Status chip). The chip/rail must
 *   REST (no is-active, no x) while the star stays - the fix must not make a non-expressing view anchor.
 *
 * CASE 8 (deep links unregressed): cold-load /item/<id>?team=<team> and /list?panel=<id> via
 *   coldload_checks.js -> both land as requested, no default applied.
 *
 * COLLAPSED-RAIL cases (1 and 2) need J.R.: the rail-collapse toggle is not drivable by tooling and
 * setCollapsed is closure-scoped. Mark them "needs manual confirmation", not verified.
 */
