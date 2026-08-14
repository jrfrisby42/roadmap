/* RPT-LAY-1 Defect 1 regression check - copy/paste browser-console snippet.
 *
 * Guards "exactly one horizontal scrollbar on the Reports table at every PAGE-scroll position, panels
 * EXPANDED". The C2 fix showed the sticky proxy from a STATIC "is the wrap bottom below the fold" check
 * computed only on render/resize; once the user scrolled the page the wrap's native bar came into view
 * while the proxy stayed pinned, so BOTH showed. RPT-LAY-1 recomputes proxy visibility on every
 * .frz-content scroll (a change-guarded single-element rect read), so the proxy hides the moment the
 * native bar scrolls into view. (A scroll listener rather than IntersectionObserver because IO/rAF
 * callbacks do not fire in a backgrounded tab, which made IO unverifiable in the headless harness.)
 *
 * WHY THIS WALKS THE RANGE INSTEAD OF SAMPLING 3 POINTS: the double-scrollbar only exists in the STRADDLE
 * zone - where the wrap's bottom edge sits just below the fold and a small scroll lifts it above. With a
 * large scroll range, sampling only {0, half, max} can jump clean over the straddle. This version WALKS
 * the full range in small steps so it lands inside the straddle no matter how big the range is, and
 * asserts exactly one bar at every step. Because Reports now DEFAULTS to collapsed panels, expand them
 * first (the defect only exists expanded).
 *
 * HOW TO RUN: open Reports on a team whose panel block is tall enough that the table overflows the viewport
 * AND is wider than its container, EXPAND the panels, then `await rptOneScrollbarCheck()`. pass=true means
 * exactly one horizontal scrollbar at every step. Reverting Defect 1 (static visibility) makes it fail
 * inside the straddle - the native bar and the proxy both show.
 */
async function rptOneScrollbarCheck(){
  var sc=document.getElementById('frzContent')||document.querySelector('.frz-content');
  var wrap=document.querySelector('.frz-rpt-tablewrap'), proxy=document.querySelector('.frz-rpt-hscroll');
  if(!(sc&&wrap&&proxy)) return {pass:false, err:'open the Reports view first'};
  var aggs=document.querySelector('.frz-rpt-aggs');
  if(!aggs || getComputedStyle(aggs).display==='none') return {pass:false, err:'expand the summary panels first (the defect only exists expanded)'};
  if(!(wrap.scrollWidth > wrap.clientWidth + 2)) return {pass:true, note:'no horizontal overflow - nothing to test'};
  var maxS=sc.scrollHeight - sc.clientHeight;
  if(maxS<=0) return {pass:true, note:'table fits in the viewport - single native bar only'};
  var step=Math.max(8, Math.round(maxS/24));   // fine enough to land inside a narrow straddle zone
  var origTop=sc.scrollTop, worst=null, checked=0;
  for(var s=0; s<=maxS; s+=step){
    sc.scrollTop=s;
    sc.dispatchEvent(new Event('scroll'));   // browsers fire this automatically on scroll in any FOREGROUND
                                             // tab; dispatch it so the check is deterministic in a
                                             // backgrounded/headless tab too. Reverted (no scroll recompute),
                                             // this is a no-op and the proxy stays wrongly visible.
    await new Promise(function(r){ setTimeout(r,25); });
    var scRect=sc.getBoundingClientRect(), wr=wrap.getBoundingClientRect();
    var nativeBarVisible = wr.bottom <= scRect.bottom + 1 && wr.bottom > scRect.top;
    var proxyVisible = getComputedStyle(proxy).display !== 'none';
    var bars=(nativeBarVisible?1:0)+(proxyVisible?1:0);
    checked++;
    if(bars!==1 && (!worst || bars>worst.visibleBars)) worst={scrollTop:s, nativeBar:nativeBarVisible, proxy:proxyVisible, visibleBars:bars, wrap_bottom:Math.round(wr.bottom), fold:Math.round(scRect.bottom)};
  }
  sc.scrollTop=origTop; sc.dispatchEvent(new Event('scroll'));
  var pass = worst===null;
  console.log('[rptOneScrollbarCheck] pass='+pass+' ('+checked+' steps, step='+step+'px)', worst||'one bar at every step');
  return {pass:pass, stepsChecked:checked, worstOffender:worst};
}
