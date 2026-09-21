"""CLEANUP-1 Items 4/5/6 - client-only cleanups, verified by source assertion (the roadmap.html grep style
used by test_item_composer). Item 4: tsMapRow no longer stringifies a placeholder OBJECT into a mapping.
Item 5: a disabled Save/Create button is visibly dimmed. Item 6: the #fff8f0 caution tint is a token."""
import os

_HTML = None


def _html():
    global _HTML
    if _HTML is None:
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "roadmap.html")
        with open(p, encoding="utf-8") as f:
            _HTML = f.read()
    return _HTML


# ── Item 4: the "[object Object]" mapping bug ───────────────────────────────────────────────────────────
def test_item4_tsmaprow_no_longer_stringifies_object():
    h = _html()
    assert "escA(o.value||o)" not in h                 # tsMapRow's buggy value fallback is gone
    # the normalisation guard is present in tsMapRow
    assert "(o && typeof o === 'object')" in h


# ── Item 5: disabled button visible state ───────────────────────────────────────────────────────────────
def test_item5_disabled_save_button_is_dimmed():
    h = _html()
    import re
    m = re.search(r"#saveBtn:disabled\s*\{([^}]*)\}", h)
    assert m, "no #saveBtn:disabled rule"
    body = m.group(1).replace(" ", "")
    assert "opacity:.55" in body and "cursor:not-allowed" in body


# ── Item 6: the caution tint is a token ─────────────────────────────────────────────────────────────────
def test_item6_caution_tint_tokenised():
    h = _html()
    assert "--caution-bg: #fff8f0;" in h               # token defined on :root, light value unchanged
    assert h.count("background:#fff8f0") == 0          # no literal left
    assert h.count("background:var(--caution-bg)") == 6  # the six repointed sites


def test_item6_no_dark_override_so_no_visual_change():
    # the refactor is value-preserving: the token is defined once (:root), with NO body.dark-mode override,
    # so it resolves to #fff8f0 in both themes exactly as the literals did.
    h = _html()
    assert h.count("--caution-bg:") == 1               # single definition; a dark override would be a 2nd
