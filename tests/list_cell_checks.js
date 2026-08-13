/* LIST-CELL-1 regression check - copy/paste browser-console snippet.
 *
 * Guards the fix for the panel's inline status change mistargeting a row cell. The panel patches the
 * open List row's status cell live (_frzUpdateRowStatus); the bug was that it selected the cell by a
 * hardcoded positional index (td[4]) which lands on the wrong cell whenever the visible columns before
 * Status differ from the default (e.g. Description shown -> chip landed in Description). The fix targets
 * td[data-col="status"] instead. _frzUpdateRowStatus is closure-scoped (not reachable from globals), so
 * this check drives the REAL panel and asserts the resulting DOM - it needs no access to the function.
 *
 * HOW TO RUN: open the List view on an SLA-enabled team with at least one item, set the column
 * visibility you want to test (Columns picker, or localStorage frazil_beta_listcols_<user>), reload,
 * then paste and `await listCellStatusCheck()`. pass=true means the new status chip landed in the
 * Status cell and NOWHERE else. Reverting the fix (back to td[4]) makes it fail with Description shown.
 */
async function listCellStatusCheck(target){
  var lb = document.getElementById('listBody');
  var row = lb && lb.querySelector('tr.list-row');
  if(!row) return {pass:false, err:'no list row (open the List view first)'};
  // open the detail panel (click the Name cell; fall back to the row)
  (row.querySelector('td[data-col="name"]') || row).click();
  await new Promise(r=>setTimeout(r,700));
  var sel = document.getElementById('frzLpStatus');
  if(!sel) return {pass:false, err:'no panel status select (need editor/admin)'};
  // pick a target status different from the current one
  target = target || [].map.call(sel.options, function(o){return o.value;}).filter(function(v){return v && v!==sel.value;})[0];
  sel.value = target;
  sel.dispatchEvent(new Event('change', {bubbles:true}));
  await new Promise(r=>setTimeout(r,1300));   // API write + row patch
  row = document.getElementById('listBody').querySelector('tr.list-row');
  var statusTd = row.querySelector('td[data-col="status"]');
  var statusOk = !!(statusTd && statusTd.textContent.trim()===target && statusTd.querySelector('span[class*="s-"]'));
  // any NON-status cell now carrying the target as a status chip = a mistarget
  var stray = null;
  [].forEach.call(row.querySelectorAll('td'), function(td){
    if(td===statusTd) return;
    var sp = td.querySelector('span[class*="s-"]');
    if(sp && sp.textContent.trim()===target) stray = td.getAttribute('data-col') || td.className || 'td';
  });
  var pass = statusOk && !stray;
  console.log('[listCellStatusCheck] target='+target+' statusOk='+statusOk+' strayCell='+(stray||'none')+' pass='+pass);
  return {pass:pass, statusOk:statusOk, strayCell:stray, target:target};
}
