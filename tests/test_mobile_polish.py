"""MOBILE-POLISH-1 source-shape guards: three mobile defects fixed in roadmap.html.

D1 - card-header action labels wrap mid-word at <=640px: shortened via CSS (::after) inside the
     existing media query, with an aria-label carrying the full name so the accessible name survives.
D2 - a colour picture-frame emoji in the comment toolbar: replaced with a flat monochrome IC.image
     entry (fixes both toolbars that share the `image` button). The second colour glyph (the emoji
     button, only in the full description toolbar) is a recorded follow-up, deliberately NOT touched.
D3 - the mobile List card meta line (type . owner) mis-baselines because type was inline-flex and
     owner was bare text: both are now matching .lc-mi segments with a .lc-sep separator, so the
     absent-type case renders the same shape.

These are SOURCE-SHAPE guards - a CSS/markup change's real proof is a screenshot on a 430px device
(J.R.'s check), and the render structure was confirmed headlessly (test served roadmap.html locally,
_listCardHtml returned 2 .lc-mi + 1 .lc-sep with a type, 1 .lc-mi + 0 .lc-sep without). Each guard
fails when the fix is reverted. roadmap.html only; server.py is untouched by this stage.
"""
import re
import pathlib

ROADMAP = pathlib.Path(__file__).resolve().parent.parent / "roadmap.html"
SERVER = pathlib.Path(__file__).resolve().parent.parent / "server.py"


def _html() -> str:
    return ROADMAP.read_text(encoding="utf-8", errors="replace")


# ── D1 ────────────────────────────────────────────────────────────────────────
def test_d1_buttons_have_full_aria_labels():
    src = _html()
    assert 'id="frzLinkAdd" aria-label="Add link"' in src, "frzLinkAdd must keep its full accessible name"
    assert 'id="frzAttUp" aria-label="Upload"' in src, "frzAttUp must keep its full accessible name"
    assert 'aria-label="Create procurement request"' in src, "frzReqCreate must keep its full accessible name"


def test_d1_heading_has_hook_class():
    assert '<span class="frz-cardh-title">Attachments &amp; Links</span>' in _html(), \
        "the card heading needs the frz-cardh-title hook so CSS can keep it on one line"


def test_d1_label_shortening_is_css_after_swap():
    src = _html()
    for bid, short in (("frzLinkAdd", "Link"), ("frzAttUp", "Upload"), ("frzReqCreate", "Request")):
        assert re.search(rf"#\s*{bid}\s*\{{\s*font-size:0;\s*\}}", src), f"{bid} must zero its visible text at mobile"
        assert f'#{bid}::after {{ content:"{short}"' in src, f"{bid} must show the short label via ::after"


# ── D2 ────────────────────────────────────────────────────────────────────────
def test_d2_ic_image_entry_added():
    src = _html()
    assert re.search(r"image:'<svg class=\"frz-ic\" viewBox=\"0 0 16 16\">", src), "an IC.image flat SVG entry must exist"


def test_d2_comment_image_button_uses_ic_not_emoji():
    src = _html()
    assert "label:IC.image, title:'Insert image'" in src, "the image button must render the flat IC.image, not a literal"
    # the colour picture-frame emoji must be gone entirely
    assert "\\u{1F5BC}" not in src, "the colour picture-frame emoji must no longer appear anywhere"


def test_d2_emoji_button_left_as_followup():
    # the OTHER colour glyph (the emoji button, description-toolbar only) is a recorded follow-up,
    # deliberately not folded into this stage - it must still be present (untouched).
    assert "\\u{1F642}" in _html(), "the emoji button glyph is out of scope this stage and must be left in place"


# ── D3 ────────────────────────────────────────────────────────────────────────
def test_d3_meta_segments_are_uniform():
    src = _html()
    assert '<span class="lc-mi">${tIc}${esc(it.type)}</span>' in src, "the type segment must be a uniform .lc-mi"
    assert '<span class="lc-mi">${esc(it.dev)}</span>' in src, "the owner segment must be a matching .lc-mi (was bare text)"
    assert 'join(\'<span class="lc-sep" aria-hidden="true">\\u00b7</span>\')' in src or \
           '<span class="lc-sep" aria-hidden="true">' in src, "the separator must be a flex .lc-sep between segments"
    # the old floating-baseline markup (inline-flex type span beside bare owner) must be gone
    assert 'inline-flex;align-items:center;gap:4px">${tIc}${esc(it.type)}' not in src, "the old inline-flex type span must be gone"


def test_d3_meta_css_present():
    src = _html()
    assert re.search(r"\.frz-beta \.lc-mi \{[^}]*display:inline-flex", src), ".lc-mi styling must exist"
    assert re.search(r"\.frz-beta \.lc-sep \{[^}]*flex:0 0 auto", src), ".lc-sep styling must exist"


# ── scope guard ───────────────────────────────────────────────────────────────
def test_server_untouched_no_intake_name_change():
    # D4 was recon'd as a non-defect (a genuinely uuid-named file, not a display or stored bug), so
    # this stage changes NO server-side attachment naming. Sanity: the intake name store is unchanged.
    assert '"name": (a.get("name") or "file")[:200]' in SERVER.read_text(encoding="utf-8", errors="replace")
