"""ITEM-RAIL-1 Part 2 regression guard: openItem must toggle .frz-ia (the flag-on IA gate).

Under a flag-on account (topbarViews), the rail VIEWS section is hidden by the CSS rule
`.frz-beta.frz-ia #frzViewNav {display:none}` - purely class-driven on .frz-ia. That class is toggled by
every view render (setView/setHome/setAdmin/setCalendar/setReports) but openItem never did, so the item
page rendered flag-OFF chrome (rail VIEWS reappeared). The fix: openItem toggles .frz-ia beside its
existing root.classList.add('on-item'), bringing the item route under the same gate.

This is a SOURCE-SHAPE guard: it proves the toggle line exists at openItem's on-item add. It does NOT
prove #frzViewNav is hidden or the top bar looks right - those are the live/screenshot cases in
tests/item_rail_checks.js and the post-deploy pass. Fails when Part 2 is reverted (demonstrated vs
HEAD = 6.36.2).
"""
import re
import pathlib

ROADMAP_HTML = pathlib.Path(__file__).resolve().parent.parent / "roadmap.html"


def _src() -> str:
    return ROADMAP_HTML.read_text(encoding="utf-8", errors="replace")


def test_openitem_toggles_frz_ia_beside_on_item():
    # the .frz-ia toggle must sit right after openItem's on-item add (the sole item entry point), using
    # _frzIA() so flag-off accounts receive false and are unchanged.
    m = re.search(
        r"root\.classList\.add\('on-item'\);(.*?)if\(root\) root\.classList\.toggle\('frz-ia', _frzIA\(\)\);",
        _src(), re.DOTALL,
    )
    assert m, "openItem must toggle .frz-ia via _frzIA() beside root.classList.add('on-item')"
    # 'beside' = within a few lines (a comment block is fine); not scattered elsewhere in the function
    assert m.group(1).count("\n") < 12, "the frz-ia toggle should sit beside the on-item add, not elsewhere"


def test_toggle_is_gated_on_frzia_not_forced_on():
    # must be _frzIA()-gated (flag-off safe), never a bare add('frz-ia')
    src = _src()
    # the only add/toggle of frz-ia in openItem's vicinity is the gated toggle; guard against a forced add
    m = re.search(r"function openItem\(id, opts\)\{(.*?)\n  function ", src, re.DOTALL)
    assert m, "openItem body not found"
    assert "classList.add('frz-ia')" not in m.group(1), "must not force frz-ia on (flag-off would break)"
