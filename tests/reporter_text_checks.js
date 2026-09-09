/* REPORTER-TEXT-1 check - copy/paste browser-console snippet (the thirteen Part-4 cases).
 *
 * A free-text substring filter on `reporter` (server-side, list_items?reporter=<text>), surfaced as a
 * free-text + Filter chip. Assert POSITIVE outcomes. Do NOT print email addresses - refer to subjects by
 * role/count. On the List, in the Flow shell.
 *
 * SETUP: add the chip via + Filter (or paste /list?reporter=<localpart>), then run the helpers.
 */
function _has(p){ return new URLSearchParams(location.search).has(p); }
function _val(p){ return new URLSearchParams(location.search).get(p); }
function _tab(n){ return [...document.querySelectorAll('#frzBeta .frz-tab')].find(t=>new RegExp('^'+n,'i').test(t.textContent.trim())); }

/* Case 1/2/4/5: server-side match count for a query term (page_size high so it is NOT page-scoped).
 * Compute the expected set independently (e.g. from /api/all) before trusting this. */
async function reporterCount(term){
  const t=localStorage.getItem('frazil_token'), tm=localStorage.getItem('frazil_rm_team');
  const d=await fetch('/api/items?reporter='+encodeURIComponent(term)+'&page_size=1000',{headers:{'Authorization':'Bearer '+t,'X-Team':tm}}).then(r=>r.json());
  return { term_len:term.length, total:d.total, returned:(d.items||[]).length,
           note:'compare total to an independently computed expected count; case 5 = total should exceed one page' };
}

/* Case 6/7/8: apply via URL, round-trip, clear. */
async function reporterRoundTrip(){
  const start=_val('reporter');
  _tab('Gantt').click(); await new Promise(r=>setTimeout(r,1400));
  const onGantt=_val('reporter');
  _tab('List').click(); await new Promise(r=>setTimeout(r,1500));
  const back=_val('reporter');
  return { start_present:start!=null, carried_onGantt:onGantt!=null && onGantt===start, back_intact:back===start,
           reporterFilterVar:(window.state&&state.reporterFilter) };
}

/* Case 8: the chip x actually clears (params gone, not "menu did not open"). */
function reporterClearViaX(){
  const chip=[...document.querySelectorAll('#frzFilterBar .frz-chip')].find(c=>/Reporter/.test(c.textContent));
  if(!chip) return 'no reporter chip present';
  const x=chip.querySelector('.frz-chip-clear,[data-clear]');
  if(!x) return 'reporter chip has no x';
  x.click();
  return { clicked:true, paramGoneAfter: setTimeout(()=>_has('reporter'),100)!==undefined, checkNow_reporterInUrl:_has('reporter') };
}

/* Case 12: resting on a view that cannot express it - the reporter chip should be absent on Gantt (rests),
 * while the param rides in the URL. */
function reporterRestingOnGantt(){
  return { onView:(window.state&&state.view), reporterInUrl:_has('reporter'),
           reporterChipInBar: !![...document.querySelectorAll('#frzFilterBar .frz-chip')].find(c=>/Reporter/.test(c.textContent)) };
}

/* Case 13: collapsed-rail chip row with reporter active - drive via the hook. */
async function reporterCollapsed(){
  window._frzSetCollapsed(true); await new Promise(r=>setTimeout(r,900));
  const chip=[...document.querySelectorAll('#frzFilterBar .frz-chip')].find(c=>/Reporter/.test(c.textContent));
  const out={ collapsed:window._frzIsCollapsed(), reporterChipPresent:!!chip, chipHasValue: chip?/[A-Za-z0-9]/.test(chip.textContent.replace('Reporter','')):false };
  window._frzSetCollapsed(false);
  return out;
}
