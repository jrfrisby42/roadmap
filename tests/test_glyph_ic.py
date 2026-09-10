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


SERVER = pathlib.Path(__file__).resolve().parent.parent / "server.py"


def test_toolbar_frz_ic_is_sized():
    src = ROADMAP.read_text(encoding="utf-8", errors="replace")
    assert re.search(r"\.frz-beta \.frz-rte-btn svg\.frz-ic \{[^}]*width:15px[^}]*height:15px", src), \
        "an frz-ic used as a toolbar-button label must be explicitly sized (else it expands to the button width and clips to blank)"
    # the base convention is unchanged - stroke:currentColor, no width (context rules size it)
    assert ".frz-beta svg.frz-ic { stroke:currentColor; fill:none;" in src, "the base frz-ic paint convention must be unchanged"


# ── Part 2: colour-emoji chrome -> flat IC / inline SVG / plain text ──────────
def test_ic_emoji_and_link_entries_added_and_sized():
    src = ROADMAP.read_text(encoding="utf-8", errors="replace")
    assert re.search(r"emoji:'<svg class=\"frz-ic\" viewBox=\"0 0 16 16\">", src), "IC.emoji flat SVG entry must exist"
    assert re.search(r"link:'<svg class=\"frz-ic\" viewBox=\"0 0 16 16\">", src), "IC.link flat SVG entry must exist"
    # the link badge is not a sizing wrapper, so IC.link needs its own rule (the Part 1 lesson)
    assert re.search(r"\.frz-beta \.frz-att-ic svg\.frz-ic \{[^}]*width:15px", src), "IC.link's .frz-att-ic sizing rule must exist"


def test_emoji_and_link_sites_use_ic_not_emoji():
    src = ROADMAP.read_text(encoding="utf-8", errors="replace")
    assert "emoji:    { label:IC.emoji, title:'Emoji'" in src, "the emoji toolbar button must render IC.emoji, not the smiley literal"
    assert '<span class="frz-att-ic" title="Link">\'+IC.link+\'</span>' in src, "the link-row badge must render IC.link, not the chain emoji"
    assert "\\u{1F642}" not in src, "the smiley emoji literal must be gone"
    assert "\U0001F517" not in src and "🔗" not in src, "the link chain emoji must be gone"


def test_lock_is_flat_svg_not_emoji():
    src = ROADMAP.read_text(encoding="utf-8", errors="replace")
    # lockNote is classic-scope (IC not visible there), so an inline flat SVG, stroke inherits the amber
    assert "🔒" not in src, "the lock emoji must be gone"
    assert 'width="11" height="11" fill="none" stroke="currentColor"' in src, "the lock note must render an 11px currentColor svg"
    assert '<rect x="3.5" y="7.2" width="9" height="6" rx="1.2"/><path d="M5.5 7.2V5.2a2.5 2.5 0 0 1 5 0v2"/></svg> Locked - active item' in src, \
        "the lock note must render an inline flat lock (body + shackle) before the text"


def test_pushpin_help_text_reworded_no_emoji():
    src = ROADMAP.read_text(encoding="utf-8", errors="replace")
    assert "📌" not in src, "the pushpin emoji must be gone from the help text"
    assert "(a pin badge shows this)" in src, "the help text must describe the real flat pin badge"


def test_server_headings_are_plain_text():
    src = SERVER.read_text(encoding="utf-8", errors="replace")
    assert "<h1>Access Denied</h1>" in src and "⛔" not in src, "the Access Denied heading must be plain text"
    assert "<h1>Audit Log - {team}</h1>" in src and "⚙" not in src, "the Audit Log heading must be plain text"


def test_class_f_star_and_flag_untouched():
    src = ROADMAP.read_text(encoding="utf-8", errors="replace")
    # the saved-filter/default markers (Class F, load-bearing across 5 paint regions) must be intact
    assert 'title="Default board">★' in src, "the default-board star must still render (Class F, never touched)"
    assert "_SAVED_IC = IC.flag" in src, "IC.flag must not be renamed/aliased (2.4)"
    assert "var _FRZ_EMOJI=" in src, "the emoji picker palette (_FRZ_EMOJI) must be untouched (feature data)"
