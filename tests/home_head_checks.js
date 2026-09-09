/* HOME-HEAD-1 check - copy/paste browser-console snippet (alignment layer).
 *
 * Fix: My Home's header is now built from the same effective columns its rows use, so every header cell's
 * label sits over the column it names. Source-shape tests prove the header is derived; only this snippet
 * proves the LABELS LINE UP (identity, not count). Run it on each _listRowHtml tab.
 *
 * HOW TO RUN: open My Home on a Jira-mapped scope (state.project = Fraznet or __all__), un-hide Reporter,
 * Created, Space and Jira in a List Columns picker first, then on each tab (Assigned/Watching/Recent) run
 * homeHeadAlign(). Expect ok:true and mism:[] . Then re-order a column and re-run (the header must follow).
 */
function homeHeadAlign(){
  var hr=document.getElementById('frzHomeHeadRow');
  var row=document.querySelector('#frzHomeBody tr.list-row');
  if(!hr||!row) return 'need a My Home list tab with at least one row';
  // The header is built from _listEffectiveCols in order; the body renders the SAME array in order. So the
  // primary assertion is: header data-cols (minus the lead cell) === _listEffectiveCols(), exactly.
  var hCols=[...hr.children].slice(1).map(th=>th.getAttribute('data-col'));
  var eff=(typeof _listEffectiveCols==='function')?_listEffectiveCols():[];
  var headerMatchesEff = JSON.stringify(hCols)===JSON.stringify(eff);
  // Secondary: NOT every body <td> carries data-col (key/type/priority/owner/assignee don't). For those
  // body cells that DO, confirm they sit under the header cell of the same data-col, at the same position.
  var bCols=[...row.children].slice(1).map(td=>td.getAttribute('data-col'));
  var mism=[];
  bCols.forEach(function(c,i){ if(c && hCols[i]!==c) mism.push({pos:i, header:hCols[i]||'(none)', cell:c}); });
  var out={ tab:(document.querySelector('#frzHomeTabs .is-active')||{}).textContent||'?',
            headerCols:hCols, effectiveCols:eff, headerMatchesEffective:headerMatchesEff,
            dataColCellMismatches:mism, ok: headerMatchesEff && mism.length===0,
            // spot-fix demonstrations: the two known-bad labels must now be right (not Pts / Sprint)
            reporterLabel: labelOver('reporter'), jiraLabel: labelOver('jira') };
  console.log('[homeHeadAlign] '+JSON.stringify(out));
  return out;
  function labelOver(col){ var th=hr.querySelector('th[data-col="'+col+'"]'); return th?th.textContent.trim():'(column not shown)'; }
}

/* Geometry check: My Home row height + column widths must be unchanged (35px rows measured pre-fix).
 * Run homeHeadGeom() and compare the numbers to the pre-fix capture. */
function homeHeadGeom(){
  var rows=[...document.querySelectorAll('#frzHomeBody tr.list-row')].slice(0,8);
  var hr=document.getElementById('frzHomeHeadRow');
  return { rowHeights: rows.map(r=>r.offsetHeight),
           headerHeight: hr?hr.offsetHeight:'n/a',
           colWidths: [...(rows[0]?rows[0].children:[])].map(td=>({col:td.getAttribute('data-col')||'(lead)', w:Math.round(td.getBoundingClientRect().width)})) };
}
