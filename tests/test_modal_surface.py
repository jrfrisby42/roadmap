"""MODAL-SURFACE-1 guards: comment-box height (Part A) + dark-mode elevation repoint (Part B).

Part A: the collapsed comment placeholder matches the mounted EDITABLE (~62px, not the removed 137px whole
block), and every comment-toolbar button shares one authoritative height (the image button's svg no longer
makes it taller - the .frz-ic sizing tax). Part B: repoint the modal elevation bridge so classic modals read
--frz-bg-elevated instead of --frz-bg-app (they lifted from the app body to one clear step above it), with
the COUPLED dark --frz-text-muted lift (so muted text keeps WCAG AA on the now-lighter modal), and a
dark-only light-hairline elevation cue (a black/navy shadow is inert on a dark ground).

Source-shape guards; all colour/height claims were measured element-level in a running browser (dark modal
0.0232 vs body 0.0074; muted #9AA1AB on the modal = 5.51; all toolbar buttons 29px). server.py untouched.
These fail on revert. Out of scope and asserted untouched here: the #fff8f0 caution tint, badge colours.
"""
import re
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = ROOT / "roadmap.html"
SERVER = ROOT / "server.py"


def _html():
    return HTML.read_text(encoding="utf-8", errors="replace")


# ── Part A ───────────────────────────────────────────────────────────────────────────────────────────────
def test_modalsurface_placeholder_matches_editable_not_block():
    src = _html()
    # placeholder reserves ONLY the editable height (~62px), on the placeholder, NOT the container
    assert ".modal.frz-composer #composerCommentComposer .composer-cmt-ph { min-height: 62px; }" in src, \
        "the collapsed placeholder must match the mounted editable (62px), so the box does not step on click"
    # the removed 137px whole-block reserve must NOT return, on placeholder or container
    assert "composer-cmt-ph { min-height: 137px;" not in src and "#composerCommentComposer { min-height: 137px;" not in src, \
        "the 137px whole-block reserve must stay removed (62 matches the editable; 137 matched toolbar+button too)"
    # the comment records both numbers so the next reader does not read it as a flip-flop
    assert "NOT the removed 137px reserve" in src and "matches only the editable" in src, \
        "a comment must record why 62 is right and 137 was not"


def test_modalsurface_toolbar_one_height():
    src = _html()
    assert re.search(r"\.modal\.frz-composer \.frz-rte-tb-comment \.frz-rte-btn \{ height: 29px; display: inline-flex; align-items: center;", src), \
        "every comment-toolbar button must share one authoritative height (fixed height + flex-centre)"


# ── Part B: the bridge repoint + coupled contrast + elevation cue ─────────────────────────────────────────
def test_modalsurface_bridge_repointed():
    src = _html()
    m = re.search(r"body\.frz-beta-active \.modal-bg \{.*?\n  \}", src, re.DOTALL)
    assert m, "the modal elevation bridge not found"
    b = m.group(0)
    assert "--surface:var(--frz-bg-elevated);" in b, "modals must read --frz-bg-elevated (lifted above the body), not --frz-bg-app"
    assert "--surface2:var(--frz-bg-surface);" in b, "--surface2 (recessed inner surfaces) must read one rung below the modal"
    assert "--frz-bg-app" not in b.split("--surface:")[1].split(";")[0], "the ground rung must no longer be the modal surface"


def test_modalsurface_muted_lift_coupled():
    src = _html()
    # the coupling: the dark muted token is lifted (both dark definitions) so AA holds on the elevated modal
    assert src.count("--frz-text-muted:#9AA1AB;") >= 2, \
        "both dark --frz-text-muted definitions must be lifted to #9AA1AB (coupled with the bridge repoint)"
    assert "--frz-text-muted:#8A9099" not in src, "the old dark muted value must be fully replaced (or AA fails on elevated)"


def test_modalsurface_dark_elevation_cue():
    src = _html()
    # a dark-only light hairline (a black/navy shadow is inert on a dark ground); light shadow untouched
    assert re.search(r"body\.dark-mode \.modal-bg \.modal \{[^}]*box-shadow: 0 0 0 1px rgba\(255,255,255,0\.06\)", src), \
        "the dark modal must carry a light-hairline elevation cue"


# ── Invariants ───────────────────────────────────────────────────────────────────────────────────────────
def test_modalsurface_invariants():
    src = _html()
    # the shell elevation token VALUES are unchanged (only which token the modal points at changed)
    assert "--frz-bg-elevated:#232A3A;" in src and "--frz-bg-app:#11151D;" in src and "--frz-bg-surface:#1C2230;" in src, \
        "the --frz-bg-* dark ladder values (rail/topbar/cards/shell) must be unchanged"
    # the #fff8f0 caution tint is OUT OF SCOPE and must be untouched (still present, not tokenised here)
    assert "background:#fff8f0" in src, "the #fff8f0 caution tint must be left untouched (separate change)"
    # client-only stage
    py = SERVER.read_text(encoding="utf-8", errors="replace")
    assert "frz-bg-elevated" not in py and "composer-cmt-ph" not in py, "MODAL-SURFACE-1 is client-only; no symbol in server.py"
