"""PLAN-ACTS-LOOP-1: the "Page Unresponsive" freezes on J.R.'s loads (2026-11-18).

HANG-PROBE-2 caught it: renderPlanningBoard -> getPlanningItems, ~104s into the load. With the last view on
Planning, the board drew before the Activity Center's open set arrived. getPlanningItems asked _ensureOpenActs to
repaint "when the set lands", but while the request was in flight _ensureOpenActs resolved AT ONCE with nothing, so
the board repainted, found the set still missing, asked again... a promise loop that never yields, so the response
that would end it never arrives. Reproduced headless (open set delayed 3s, last view Planning) on 6.62.61; the fix
loads 3/3. Source guards only (behaviour verified headlessly).
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "roadmap.html").read_text(encoding="utf-8", errors="replace")


def test_a_cold_read_waits_for_the_request_in_flight():
    body = HTML.split("function _ensureOpenActs(){", 1)[1].split("\n}", 1)[0]
    assert "if(_openActsLoading && _openActsPromise) return _openActsPromise;" in body
    assert "Promise.resolve(_openActs || [])" not in body, "never resolve with nothing while a request is in flight"
    assert "return _openActsPromise = API.get('/api/activities?status=open')" in HTML


def test_callers_repaint_only_once_the_set_is_there():
    assert "if(_openActs === null && !_planActsWait){ _planActsWait = true; _ensureOpenActs().then(()=>{ _planActsWait = false; if(_openActs !== null && viewMode==='planning') renderPlanningBoard(); }); }" in HTML
    assert "if(!_openReady && !_dashActsWait){ _dashActsWait = true; _ensureOpenActs().then(()=>{ _dashActsWait = false; if(_openActs !== null && viewMode==='dashboard') renderDashboard(); }); }" in HTML
    assert "_ensureOpenActs().then(()=>{ if(_openActs !== null && document.getElementById('acPaneOpen')) renderAcOpen(); });" in HTML
    assert "_ensureOpenActs().then(()=>{ if(_openActs !== null) applySavedView(viewId); });" in HTML
