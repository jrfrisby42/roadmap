/* FIELDS-CARRY-2 check - copy/paste browser-console snippet (cross-view carry + anti-resurrection).
 *
 * The fix carries location/resolutionType/blockedReason across views (like priority/dept). Needs a team
 * WITH FIELDS-2 config to exercise the three directly (development is empty -> use owner/priority as the
 * carry/_selfSer controls, or add a reversible fixture value). Assert POSITIVE outcomes, never "no throw".
 *
 * RUN carryRoundTrip('blockedReason','Vendor') on a team where that value exists. It records the param at
 * each hop and whether it survived. Use carryRoundTrip('owner', <a real owner>) as the control everywhere.
 */
function _has(p){ return new URLSearchParams(location.search).has(p); }
function _val(p){ return new URLSearchParams(location.search).get(p); }
function _tab(name){ return [...document.querySelectorAll('#frzBeta .frz-tab')].find(t=>new RegExp('^'+name,'i').test(t.textContent.trim())); }
async function carryRoundTrip(param, expectVal){
  // assumes you are on the List with `param` applied (URL has it). Round-trips List -> Gantt -> List.
  var start={ view:'list', has:_has(param), val:_val(param) };
  _tab('Gantt').click(); await new Promise(r=>setTimeout(r,1400));
  var mid={ view:'gantt', has:_has(param), val:_val(param) };     // carried verbatim (Gantt cannot express it)
  _tab('List').click();  await new Promise(r=>setTimeout(r,1400));
  var end={ view:'list', has:_has(param), val:_val(param) };
  var out={ param:param, start:start, gantt:mid, back:end,
            SURVIVED: end.has && end.val===(expectVal||start.val),
            note:'carried should keep the value at every hop; before the fix it was gone by gantt' };
  console.log('[carryRoundTrip] '+JSON.stringify(out));
  return out;
}

/* Anti-resurrection: clear the chip on the List, then round-trip - it must STAY cleared (this is what
 * _selfSer guarantees). Precondition: `param` is currently applied on the List. */
async function clearStaysCleared(param){
  // clear via the chip's × (find the active chip for this param in the bar)
  var chip=[...document.querySelectorAll('#frzFilterBar .frz-chip')].find(function(c){ return c.getAttribute('data-chip')===param || new RegExp(param,'i').test(c.className); });
  if(chip){ var x=chip.querySelector('.frz-chip-clear,[class*="clear"]'); if(x) x.click(); }
  await new Promise(r=>setTimeout(r,900));
  var afterClear=_has(param);   // expect false
  _tab('Gantt').click(); await new Promise(r=>setTimeout(r,1200));
  _tab('List').click();  await new Promise(r=>setTimeout(r,1300));
  var afterRoundTrip=_has(param);   // expect false - NOT resurrected
  var out={ param:param, clearedFromUrl:!afterClear, stayedClearedAfterRoundTrip:!afterRoundTrip,
            PASS: !afterClear && !afterRoundTrip };
  console.log('[clearStaysCleared] '+JSON.stringify(out));
  return out;
}
