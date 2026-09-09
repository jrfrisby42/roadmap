/* COLLAPSE-HOOK-1 check - copy/paste browser-console snippet (the equivalence + 8 cases, then the payoff).
 *
 * The hooks route through the SAME setCollapsed the button reaches, so equivalence is guaranteed by
 * construction; this snippet confirms it empirically where the harness can, and marks the real-click half
 * for J.R. where it cannot (the toggle is not drivable by tooling - that is why the hooks exist).
 *
 * RUN collapseHookCases() in the Flow shell. For the equivalence half, if you (J.R.) can drive a real
 * #frzCollapse click, snapshot with collapseSnap() before/after and compare to a _frzSetCollapsed run.
 */
function collapseSnap(){
  var root=document.getElementById('frzBeta');
  return {
    isCollapsed: (typeof window._frzIsCollapsed==='function')?window._frzIsCollapsed():'no-hook',
    railHidden: !!root&&root.classList.contains('frz-rail-hidden'),   // flag-on
    collapsedUi: !!root&&root.classList.contains('is-collapsed-ui'),  // flag-off
    gridCols: root?getComputedStyle(root).gridTemplateColumns:null,
    barSavedChip: !!document.querySelector('#frzFilterBar .frz-chip[data-saved], #frzFilterBar .frz-saved-chip, #frzFilterBar [class*="saved"]'),
    railPref: (function(){ try{ return localStorage.getItem('frazil_beta_rail_'+localStorage.getItem('frazil_rm_user')); }catch(e){ return '?'; } })()
  };
}
function collapseHookCases(){
  if(typeof window._frzSetCollapsed!=='function'){ return 'HOOK ABSENT - not deployed here'; }
  var out={};
  // 1/2: set true collapses + reads true; set false expands + reads false
  window._frzSetCollapsed(true);  out.case1 = window._frzIsCollapsed()===true;
  window._frzSetCollapsed(false); out.case2 = window._frzIsCollapsed()===false;
  // 3: idempotent - true twice = one collapsed state, no error
  window._frzSetCollapsed(true); var a=collapseSnap(); window._frzSetCollapsed(true); var b=collapseSnap();
  out.case3_idempotent = JSON.stringify(a)===JSON.stringify(b) && a.isCollapsed===true;
  // 4/5: which shell class appears (flag-on rail-hidden vs flag-off is-collapsed-ui) - depends on _frzIA()
  var s=collapseSnap();
  out.case4_or_5_shell = s.railHidden?'flag-on: frz-rail-hidden + grid '+s.gridCols : (s.collapsedUi?'flag-off: is-collapsed-ui':'NEITHER (unexpected)');
  // 6: persisted preference written (reload to confirm it survives - manual)
  out.case6_railPref_written = s.railPref;   // expect 'c' when collapsed
  window._frzSetCollapsed(false);   // leave expanded
  console.log('[collapseHookCases] '+JSON.stringify(out));
  return out;
}

/* PAYOFF 2 - savedClearClearsAfterSwap: collapse, apply a saved filter, force a content repaint, click the
 * collapsed bar Saved chip's ×, assert the filter actually cleared. Run with the rail collapsed via the hook.
 * (Screenshots for payoff 1/3/4 are taken by hand in the live pass - element queries do not prove visibility.) */
function savedClearClearsAfterSwap(){
  window._frzSetCollapsed(true);
  var chip=document.querySelector('#frzFilterBar .frz-chip[data-saved], #frzFilterBar [class*="saved"]');
  if(!chip) return 'no collapsed Saved chip present - apply a saved filter first';
  var x=chip.querySelector('.frz-chip-clear, [class*="clear"], button');
  if(!x) return 'Saved chip has no × control';
  var before=chip.textContent.trim();
  x.click();
  return { clickedX:true, chipTextBefore:before, chipStillPresent: !!document.querySelector('#frzFilterBar .frz-chip[data-saved]'), note:'assert the filter cleared (chip gone / filter reset)' };
}
