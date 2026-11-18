"""TAB-FIT-1: the top-bar view tabs show every tab that fits (J.R. 2026-11-18: with the left menu collapsed,
Planning sat under More though there was room). The window-width bands are now a minimum; _frzFitTabs measures
the tabs and the room #frzLeft has, re-checked on resize (which a rail collapse fires). Source guards only
(verified headless: 1440px shows all 8 views open or collapsed; 1100px shows 6 + More open, all 8 collapsed)."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "roadmap.html").read_text(encoding="utf-8", errors="replace")


def test_bands_are_a_minimum_and_the_fit_only_raises_them():
    lay = HTML.split("function _frzTabLayout(){", 1)[1].split("\n  }", 1)[0]
    assert "if(_frzTabFitN != null && _frzTabFitN > lay.slots) lay.slots = _frzTabFitN;" in lay
    assert "{slots:5, iconOnly:false}" in lay and "{slots:7, iconOnly:false}" in lay


def test_fit_measures_the_real_room_and_keeps_the_active_view():
    fit = HTML.split("function _frzFitTabs(){", 1)[1].split("\n  }\n", 1)[0]
    assert "avail = left.clientWidth" in fit and "_frzTabW[v+sfx] = b.offsetWidth" in fit
    assert "vis[n-1] = active" in fit, "the active view always shows"
    assert "if(n === _frzTabFitN) return;" in fit, "no re-render once settled (no loop)"
    assert "if(!left || !left.offsetWidth) return;" in fit, "a hidden bar measures 0 - wait for a later draw"


def test_fit_runs_after_every_strip_draw_and_on_resize():
    assert "_frzScheduleTabFit();   // TAB-FIT-1: once it is on screen, show every tab that fits\n    return html;" in HTML
    assert "_frzScheduleTabFit();   // TAB-FIT-1: the room changed" in HTML
    # a rail collapse fires resize, so it re-fits too
    assert "try { window.dispatchEvent(new Event('resize')); } catch(e){}" in HTML
