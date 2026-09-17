"""TYPE-SCALE-1 Phase 0 guard: the six-tier type scale tokens are defined on :root (roadmap.html only).

Phase 0 is additive - it defines twelve tokens (six --fs-* sizes + six --lh-* line heights) on the bare
:root so they resolve globally (shell, classic <body>-child modals, and classic mode alike; the --frz-*
tokens are .frz-beta-scoped and would not reach a classic modal). NOTHING consumes them yet, so the
rendered output is unchanged - verified at source by a zero-consumers check here, and element-level in
the browser (histogram identical; the tokens read back from getComputedStyle with the values below).
--fs-body is 14 now but is not consumed until Phase 3 (unauthorized). server.py untouched.
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


def _html():
    return HTML.read_text(encoding="utf-8", errors="replace")


def test_typescale_tokens_defined():
    src = _html()
    for name, val in TOKENS.items():
        assert re.search(re.escape(name) + r"\s*:\s*" + re.escape(val) + r"\s*;", src), \
            f"type-scale token {name}: {val} must be defined"


def test_typescale_tokens_on_bare_root():
    """They must live on the bare :root (global), not the .frz-beta scope, so classic modals reach them."""
    src = _html()
    root = src.split(":root {", 1)[1].split("}", 1)[0]
    for name in TOKENS:
        assert name in root, f"{name} must be defined on the bare :root block (global reach)"


def test_typescale_phase0_zero_consumers():
    """Phase 0 is additive: NOTHING consumes the tokens yet, so the render is provably unchanged.
    (Phases 1-3 introduce consumers; update this guard when Phase 1/2 legitimately add var(--lh-*)/var(--fs-*).)"""
    src = _html()
    consumers = re.findall(r"var\(--(?:fs|lh)-(?:micro|label|body|strong|title|page)\)", src)
    assert consumers == [], f"Phase 0 must have zero token consumers; found {consumers[:5]}"


def test_typescale_client_only():
    py = SERVER.read_text(encoding="utf-8", errors="replace")
    assert "--fs-body" not in py and "--lh-body" not in py, "TYPE-SCALE-1 is client-only; no token in server.py"
