"""TYPE-SCALE-1 guards (roadmap.html only). Phases 0-1 shipped; Phases 2-4 not begun.

Phase 0 (additive): twelve tokens (six --fs-* sizes + six --lh-* line heights) on the bare :root, so
they resolve globally (shell, classic <body>-child modals, and classic mode; --frz-* are .frz-beta-scoped).
Phase 1 (line-height + pill padding, NO font-size change): a base `line-height: var(--lh-body)` on body
(was unset -> `normal`), and the three cramped pills (.frz-viewopt, .frz-rte-btn, the composer
.frz-rte-tb-modal .frz-rte-btn) bumped toward ~30px via VERTICAL padding only. The 47 `line-height:1` and
5 `line-height:0` icon/single-glyph sites are untouched (the survey said 56+8 - that counted the substring,
which includes 1.x; the exact single-glyph set is 47+5). All size/height claims verified element-level:
histogram identical across List/item-page/Gantt; pills ~30px; svg.frz-ic h=16 unchanged; column widths
unchanged; both themes. server.py untouched.
"""
import re
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = ROOT / "roadmap.html"
SERVER = ROOT / "server.py"

TOKENS = {
    "--fs-micro": "11px", "--fs-label": "12px", "--fs-body": "14px",
    "--fs-strong": "16px", "--fs-title": "18px", "--fs-page": "22px",
    "--lh-micro": "1.4", "--lh-label": "1.35", "--lh-body": "1.45",
    "--lh-strong": "1.4", "--lh-title": "1.3", "--lh-page": "1.25",
}

# The exact single-glyph / icon line-height counts Phase 1 must not disturb (guard baseline).
LH1_COUNT = 47
LH0_COUNT = 5


def _html():
    return HTML.read_text(encoding="utf-8", errors="replace")


# ── Phase 0: the token layer ─────────────────────────────────────────────────────────────────────
def test_typescale_tokens_defined():
    src = _html()
    for name, val in TOKENS.items():
        assert re.search(re.escape(name) + r"\s*:\s*" + re.escape(val) + r"\s*;", src), \
            f"type-scale token {name}: {val} must be defined"


def test_typescale_tokens_on_bare_root():
    src = _html()
    root = src.split(":root {", 1)[1].split("}", 1)[0]
    for name in TOKENS:
        assert name in root, f"{name} must be defined on the bare :root block (global reach)"


# ── Phase 1: base line-height + pill padding; NO font-size token consumed yet ─────────────────────
def test_typescale_phase1_base_lineheight():
    src = _html()
    assert re.search(r"\bbody\s*\{[^}]*line-height:\s*var\(--lh-body\)", src), \
        "Phase 1: body must set a base line-height of var(--lh-body)"


def test_typescale_phase1_no_fs_token_consumed():
    """Phases 1-2 change NO font size, so no --fs-* token may be consumed until Phase 3 (unauthorized).
    Only line-height tokens are consumed so far."""
    src = _html()
    fs_consumers = re.findall(r"var\(--fs-(?:micro|label|body|strong|title|page)\)", src)
    assert fs_consumers == [], f"no --fs-* token may be consumed before Phase 3; found {fs_consumers[:5]}"


def test_typescale_phase1_pills_bumped():
    """The three cramped pills are padded toward ~30px. The padding DIFFERS by design: .frz-viewopt
    (font 12.5 + inherited lh 1.45) needs 5px to hit ~30, while the .frz-rte-btn family (font 12 + lh 1.4)
    needs 6px - same target, different metrics, so the padding is deliberately not uniform across families."""
    src = _html()
    checks = {
        ".frz-beta .frz-viewopt {": r"padding:\s*5px",
        ".frz-beta .frz-rte-btn {": r"padding:\s*6px",
        "body.frz-beta-active .frz-rte-tb-modal .frz-rte-btn {": r"padding:\s*6px",
    }
    for sel_fragment, pat in checks.items():
        rule = src.split(sel_fragment, 1)[1].split("}", 1)[0]
        assert re.search(pat, rule), f"Phase 1: pill `{sel_fragment}` padding must match {pat}. Rule: {rule[:80]}"


def test_typescale_phase1_icon_lineheight_intact():
    """The load-bearing Phase 1 guard: the base body line-height must NOT knock over the single-glyph /
    icon line-height:1 / :0 sites (this codebase has paid an icon-alignment tax four times). Their exact
    counts must be unchanged."""
    src = _html()
    n1 = len(re.findall(r"line-height:\s*1(?:\s*[;}!]|\s*$)", src, re.M))
    n0 = len(re.findall(r"line-height:\s*0(?:\s*[;}!]|\s*$)", src, re.M))
    assert n1 == LH1_COUNT, f"line-height:1 (icon geometry) count must stay {LH1_COUNT}, got {n1}"
    assert n0 == LH0_COUNT, f"line-height:0 (icon geometry) count must stay {LH0_COUNT}, got {n0}"


def test_typescale_client_only():
    py = SERVER.read_text(encoding="utf-8", errors="replace")
    assert "--fs-body" not in py and "--lh-body" not in py, "TYPE-SCALE-1 is client-only; no token in server.py"
