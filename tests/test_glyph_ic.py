"""GLYPH-IC-1 Part 1 guard: IC.image (MOBILE-POLISH-1 D2) must be SIZED in the toolbar.

MOBILE-POLISH-1 shipped IC.image as a `.frz-rte-btn` label with no sizing wrapper. Every other frz-ic
site sizes its svg via a context rule (.frz-tab-ic/.frz-title-ic/.frz-bell/.frz-watch), but the base
`.frz-ic` rule sets none - so the unwrapped toolbar svg blew up to the button width (~214px measured
live) and clipped to blank. Part 1 adds the matching context rule.

This is a SOURCE-SHAPE guard - it proves the sizing rule exists, NOT that the icon is visible (that
was screenshot-confirmed live; an element query is exactly what let the blank icon ship). It fails
when Part 1 is reverted. roadmap.html only.
"""
import re
import pathlib

ROADMAP = pathlib.Path(__file__).resolve().parent.parent / "roadmap.html"


def test_toolbar_frz_ic_is_sized():
    src = ROADMAP.read_text(encoding="utf-8", errors="replace")
    assert re.search(r"\.frz-beta \.frz-rte-btn svg\.frz-ic \{[^}]*width:15px[^}]*height:15px", src), \
        "an frz-ic used as a toolbar-button label must be explicitly sized (else it expands to the button width and clips to blank)"
    # the base convention is unchanged - stroke:currentColor, no width (context rules size it)
    assert ".frz-beta svg.frz-ic { stroke:currentColor; fill:none;" in src, "the base frz-ic paint convention must be unchanged"
