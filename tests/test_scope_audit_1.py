"""SCOPE-AUDIT-1 Part A guard: modal fields visibly distinct in dark (roadmap.html only).

In dark the recessed field rung (var(--surface2) = --frz-bg-surface, 0.0160) sat at only 1.11 WCAG contrast
from the modal (0.0232) - genuinely a different colour but below the threshold a human reads, so fields looked
identical. Fix: a MODAL-SCOPED override deepens the field fill (--surface2 -> #0E121A, 1.31 vs the modal) and
strengthens the field border (--border -> #4C525C, rgb 76,82,92; 1.82 vs the modal, 2.38 vs the field) so the
borderless title (#fName, border only on the bottom) and every field read as boxes. Scoped to
body.dark-mode .modal-bg so the shell rung --frz-bg-surface (topbar/cards) and shell borders (--frz-border on
cards/rail/tables) are untouched. Placed AFTER the bridge so it wins the cascade. Dark only.

Source-shape guard; all colour claims were measured element-level in dark (field fill 1.31, border 2.38/1.82,
#fName fill 1.31 + 3px underline 1.82; shell topbar 0.0160 / rail 0.0109 unchanged; light + badges unchanged).
server.py untouched.
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = ROOT / "roadmap.html"
SERVER = ROOT / "server.py"


def _html():
    return HTML.read_text(encoding="utf-8", errors="replace")


def test_scopeaudit1_modal_field_fill_and_border():
    src = _html()
    override = "body.dark-mode .modal-bg { --surface2: #0E121A; --border: #4C525C; }"
    assert override in src, "the modal-scoped dark field-fill (#0E121A) + border (#4C525C) override must be present"
    # it must sit AFTER the bridge (same specificity) so it wins the cascade
    i_bridge = src.find("body.frz-beta-active .modal-bg {")
    i_override = src.find(override)
    assert i_bridge != -1 and i_override != -1 and i_override > i_bridge, \
        "the override must come after the bridge (body.frz-beta-active .modal-bg) to win the cascade"


def test_scopeaudit1_invariants():
    src = _html()
    # INVARIANT: the shell rung --frz-bg-surface (topbar/cards) is NOT deepened - only a modal-scoped override
    assert "--frz-bg-surface:#1C2230;" in src, "the dark shell rung --frz-bg-surface must be unchanged (A.3)"
    # INVARIANT: the --frz-border token is unchanged (the lift is a modal-scoped override, not a shell-wide change)
    assert "--frz-border:#3A3F47;" in src, "the dark --frz-border token must be unchanged (shell borders/cards/rail keep it)"
    # the fix is dark only (the override sits under body.dark-mode); no light rule was added for it
    assert "body.dark-mode .modal-bg { --surface2: #0E121A;" in src, "the fill/border override must be dark-only (body.dark-mode)"
    # client-only
    py = SERVER.read_text(encoding="utf-8", errors="replace")
    assert "0E121A" not in py and "4C525C" not in py, "SCOPE-AUDIT-1 Part A is client-only; no symbol in server.py"
