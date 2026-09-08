/* ITEM-RAIL-1 Part 2 regression check - copy/paste browser-console snippet (behavioural layer).
 *
 * Fix: openItem now toggles .frz-ia (like every view render), so the flag-on item page hides the rail VIEWS
 * section instead of showing flag-OFF chrome. The toggle turns on FIVE .frz-ia gates at once (Addendum A1),
 * so this snippet asserts each on the item route, under flag-on and flag-off.
 *
 * HOW TO RUN: open an item page under a flag-on account (window._topbarViews===true), then paste and run
 * itemRailGates(). All five should read as expected. Then repeat under flag-off (a non-J.R. account) and
 * confirm nothing changed. SCREENSHOTS still needed (A1.1): a computed value proves a rule fired, only a
 * screenshot proves the top bar looks right - especially .frz-topbar-left{flex} reflowing Back+crumb.
 */
function itemRailGates(){
  var root = document.getElementById('frzBeta');
  var onItem = !!root && root.classList.contains('on-item');
  var ia = !!root && root.classList.contains('frz-ia');
  var vn = document.getElementById('frzViewNav');
  var left = document.getElementById('frzLeft');
  var brand = document.getElementById('frzTopBrand');
  var topcol = document.getElementById('frzTopCollapse');
  var g = {
    onItem: onItem, frzIA_on_root: ia,
    // gate 1: rail VIEWS hidden (the intended fix)
    viewNav_display: vn ? getComputedStyle(vn).display : 'n/a',          // flag-on -> 'none'; flag-off -> not 'none'
    // gate 3: #frzLeft flex (the reflow risk - screenshot it)
    frzLeft_display: left ? getComputedStyle(left).display : 'n/a',      // flag-on -> 'flex'
    // gate 2/collapse: top brand + collapse only show when collapsed (.frz-rail-hidden.frz-topbrand-in)
    railHidden: !!root && root.classList.contains('frz-rail-hidden'),
    topBrand_display: brand ? getComputedStyle(brand).display : 'n/a',   // shown only when collapsed
    topCollapse_display: topcol ? getComputedStyle(topcol).display : 'n/a',
    // #frzLeft still holds Back + breadcrumb in BOTH flags (deliberate, unchanged)
    leftHasBack: !!(left && left.querySelector('#frzBack')),
    leftHasCrumb: !!(left && left.querySelector('.frz-crumbs')),
  };
  console.log('[itemRailGates] '+JSON.stringify(g));
  return g;
}

/* A2 - collapse from the item page (NEW surface under flag-on): with an item open, click #frzTopCollapse
 *   (when collapsed, to re-expand) or #frzCollapse (in the rail, to collapse), or press "[".
 *   - Assert the rail actually hides (root gets .frz-rail-hidden; grid column 0) and content fills, and
 *     expanding restores it. The handlers are wired (frzCollapse @ ~25961, frzTopCollapse @ ~25963 ->
 *     toggleCollapse), so a visible control that does nothing would be the POLISH-5 shape - assert it WORKS.
 *   - This is NEW on the item surface (the item page was never collapsible under flag-on before); state it.
 *
 * SIX SCREENSHOTS (A1.1), flag-on top bar showing brand + Back + breadcrumb together:
 *   1. rail expanded, SHORT item title   2. rail expanded, LONG title (breadcrumb truncation)
 *   3. rail collapsed (.frz-rail-hidden + .frz-topbrand-in live)
 *   ...and the same three under FLAG-OFF, to prove nothing moved.
 *   Report anything crowded / mis-truncated / oddly-placed rather than fixing it - top-bar layout is a
 *   design decision for J.R., not part of this one-line fix.
 */
