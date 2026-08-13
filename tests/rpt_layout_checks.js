/* RPT-LAY-1 Defect 1 regression check - copy/paste browser-console snippet.
 *
 * Guards "exactly one horizontal scrollbar on the Reports table at every PAGE-scroll position". The
 * C2 fix showed the sticky proxy based on a STATIC "is the wrap bottom below the fold" check computed
 * only on render/resize; once the user scrolled the page down, the wrap's native bar came into view
 * while the proxy stayed pinned, so BOTH showed. RPT-LAY-1 recomputes proxy visibility on every
 * .frz-content scroll (a change-guarded single-element rect read), so the proxy hides the moment the
 * native bar scrolls into view. (A scroll listener rather than IntersectionObserver because IO/rAF
 * callbacks do not fire in a backgrounded tab, which made IO unverifiable in the headless harness.)
 *
 * HOW TO RUN: open Reports on a team whose panel block is tall enough that the table overflows the
 * viewport AND wider than its container (so horizontal scroll is needed), then `await rptOneScrollbarCheck()`.
 * pass=true means exactly one horizontal scrollbar is visible at top, mid and bottom page-scroll.
 * Reverting Defect 1 (static visibility) makes it fail at the bottom position (native bar + proxy both show).
 */
async function rptOneScrollbarCheck(){
  var sc=document.getElementById('frzContent')||document.querySelector('.frz-content');
  var wrap=document.querySelector('.frz-rpt-tablewrap'), proxy=document.querySelector('.frz-rpt-hscroll');
  if(!(sc&&wrap&&proxy)) return {pass:false, err:'open the Reports view first'};
  if(!(wrap.scrollWidth > wrap.clientWidth + 2)) return {pass:true, note:'no horizontal overflow - nothing to test'};
  var out=[], positions=[0, Math.round(sc.scrollHeight/2), sc.scrollHeight];
  var origTop=sc.scrollTop;
  for(var i=0;i<positions.length;i++){
    sc.scrollTop=positions[i];
    sc.dispatchEvent(new Event('scroll'));   // browsers fire this automatically on scroll in any FOREGROUND tab;
                                             // dispatch it so the check is deterministic in a backgrounded/headless
                                             // tab too (where the browser produces no scroll event). Reverted (no
                                             // listener), this is a no-op and the proxy stays wrongly visible.
    await new Promise(function(r){ setTimeout(r,60); });
    var scRect=sc.getBoundingClientRect(), wr=wrap.getBoundingClientRect();
    var nativeBarVisible = wr.bottom <= scRect.bottom + 1 && wr.bottom > scRect.top;   // wrap bottom edge in view
    var proxyVisible = getComputedStyle(proxy).display !== 'none';
    out.push({scrollTop:positions[i], nativeBar:nativeBarVisible, proxy:proxyVisible, visibleBars:(nativeBarVisible?1:0)+(proxyVisible?1:0)});
  }
  sc.scrollTop=origTop;
  var pass = out.every(function(p){ return p.visibleBars===1; });
  console.log('[rptOneScrollbarCheck] pass='+pass, out);
  return {pass:pass, positions:out};
}
