/* SAVED-PAINT-1 regression check - copy/paste browser-console snippet (behavioural layer).
 *
 * _frzPaintSavedState() is the single entry point that paints the five saved-filter regions:
 *   R1 bar Saved chip (collapsed only)   R2 rail fdot + aria-current   R3 rail star (default)
 *   R4 crumb   R5 saved-chip dropdown (on open)
 * These functions are closure-scoped (unreachable from the console); this snippet inspects the RENDERED
 * DOM - which is what the user sees - and asserts the regions AGREE about the active saved filter, and
 * that presence is correct. Source-shape coverage is tests/test_saved_paint.py.
 *
 * HOW TO RUN: on a chip-bar view (gantt/list/kanban/reports/dashboard), paste, then after each of
 *   apply a saved filter / clear it / change Space / toggle the rail ([ shortcut)
 * run savedRegionsAgree(). pass=true means every region reflects the same active filter (or all rest).
 * Run it in BOTH shells (flag-off default, flag-on = topbarViews) and BOTH rail states (expanded/collapsed).
 * Assertions are POSITIVE (5.2): the right row is lit, the crumb names the right filter, the star is on the
 * default row - not merely "nothing threw".
 */
function savedRegionsAgree(){
  var out={pass:true, checks:{}};
  function want(k,c){ out.checks[k]=c; if(!c) out.pass=false; }
  var railRows=[].slice.call(document.querySelectorAll('#frzSavedNav .frz-item[data-saved]'));
  // R2: at most one rail row is active (aria-current=true AND has the fdot); they must agree.
  var railActive=railRows.filter(function(b){ return b.getAttribute('aria-current')==='true'; });
  var railFdot=railRows.filter(function(b){ return !!b.querySelector('.frz-item-fdot'); });
  want('rail:one-or-zero-active', railActive.length<=1);
  want('rail:aria-and-fdot-agree', railActive.length===railFdot.length &&
       railActive.every(function(b){ return !!b.querySelector('.frz-item-fdot'); }));
  var activeId = railActive[0] ? railActive[0].getAttribute('data-saved') : null;
  var activeName = railActive[0] ? (railActive[0].querySelector('span')||{}).textContent : null;
  if(activeName) activeName=activeName.replace(/\s*★\s*$/,'').trim();   // strip the star suffix if the active row is also default
  // R1: bar Saved chip (collapsed only). If present + active it must name the SAME filter as the rail.
  var chip=document.querySelector('#frzFilterBar [data-chip="saved"]');
  if(chip){
    var chipActive=chip.classList.contains('is-active');
    var chipName=(chip.querySelector('.frz-chip-value')||{}).textContent||'';
    var chipClear=!!chip.querySelector('[data-savedclear]');
    if(activeId){ want('chip:active-matches-rail', chipActive && chipName.trim()===String(activeName)); want('chip:has-clear-x', chipClear); }
    else { want('chip:rests-when-none-active', !chipActive && !chipClear && /saved filters/i.test(chipName)); }
  }
  // R4: crumb - names the active filter, or does not when none/resting.
  var crumb=document.querySelector('#frzLeft .frz-crumbs');
  if(crumb){
    if(activeId) want('crumb:names-active', crumb.textContent.indexOf(String(activeName))>=0);
    // when none active, crumb has no saved-filter name segment (only the Space) - not asserted strictly (project varies)
  }
  // R3: the star sits on the DEFAULT row only (identity - independent of which is active).
  var starRows=railRows.filter(function(b){ return !!b.querySelector('.frz-saved-star'); });
  want('star:at-most-one-default', starRows.length<=1);
  console.log('[savedRegionsAgree] '+JSON.stringify(out));
  return out;
}

/* SAVED-PAINT-1 ADDENDUM A1.4: the bar Saved chip is repainted by chip.outerHTML=_savedChipHTML(), which
 * DESTROYS the node and inserts a new one. Investigation confirmed the chip's x and dropdown handlers are
 * DELEGATED on `root` (wire() -> root.addEventListener('click', ... e.target.closest('[data-savedclear]')))
 * - a stable ancestor NOT inside the replaced subtree - so they survive the swap. This check proves it
 * BEHAVIOURALLY (a source-shape guard cannot): after the chip has been swapped, click its x and assert the
 * filter ACTUALLY CLEARED - the positive outcome, not "nothing threw" and not "the menu did not open" (the
 * POLISH-5 no-op-button trap).
 *
 * HOW TO RUN: collapse the rail, apply a saved filter (so the bar Saved chip shows is-active + x), then to
 * force at least one outerHTML content-swap, change Space (or re-apply). Then paste and `await savedClearClearsAfterSwap()`.
 * pass=true means the x on the SWAPPED chip cleared the filter. If the swap had killed the handler, cleared
 * would be false (the chip would still be is-active) - that is the failing demonstration for this region.
 */
async function savedClearClearsAfterSwap(){
  var chip=document.querySelector('#frzFilterBar [data-chip="saved"]');
  if(!chip) return {pass:false, err:'no bar Saved chip - collapse the rail and apply a saved filter first'};
  if(!chip.classList.contains('is-active')) return {pass:false, err:'Saved chip is resting - apply a saved filter first'};
  var x=chip.querySelector('[data-savedclear]');
  if(!x) return {pass:false, err:'active chip has no clear x (regression: swap dropped data-savedclear)'};
  x.click();                                   // exercises the delegated handler against the post-swap node
  await new Promise(r=>setTimeout(r,300));
  var chip2=document.querySelector('#frzFilterBar [data-chip="saved"]');
  var chipCleared = !chip2 || (!chip2.classList.contains('is-active') && !chip2.querySelector('[data-savedclear]'));
  var railActive=[].slice.call(document.querySelectorAll('#frzSavedNav .frz-item[data-saved][aria-current="true"]')).length;
  var pass=chipCleared && railActive===0;      // POSITIVE outcome: filter actually gone, everywhere
  console.log('[savedClearClearsAfterSwap] chipCleared='+chipCleared+' railStillActive='+railActive+' pass='+pass);
  return {pass:pass, chipCleared:chipCleared, railActive:railActive};
}

/* MATRIX (Part 5.1): {flag-off, flag-on} x {expanded, collapsed}. For each cell, run savedRegionsAgree
 * after: apply / clear / change scope / toggle rail / save first filter while collapsed (L1) /
 * delete last filter while collapsed (mirror). Every cell: pass=true.
 *
 * HYDRATION (Part 5.3), the case that MOST needs observing and CANNOT pass on pre-fix code:
 *   empty localStorage saved cache + collapsed rail + NO default filter set + server returns N filters
 *   -> the bar Saved chip must APPEAR once the server rows arrive.
 * This requires an account with NO default saved filter, which does not exist today (blocker, shared with
 * COLDLOAD-2). Do NOT unset J.R.'s default (server-mirrored config on the daily-use account). Mark this
 * case UNVERIFIED until a no-default account exists. With a default set the pre-fix code passed it
 * incidentally (via _frzApplyDefaultFilter -> renderBar), so a default-set run does not test the fix.
 */
