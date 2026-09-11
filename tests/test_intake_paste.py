"""INTAKE-PASTE-1 guard: paste a screenshot into the public /report portal.

One document-level `paste` listener in the portal's server-rendered JS that hands pasted FILE entries
to the existing addFiles - the same path as drop and the file input. No endpoint, no new upload path,
no second copy of the 10-file guard / presign / upload / record. Text paste is untouched:
preventDefault fires ONLY when the clipboard carries file entries.

SOURCE-SHAPE guard (server.py). A paste event is hard to synthesise convincingly, so the definitive
check is a human pasting into the real form (J.R. post-deploy). This fails when the listener is
reverted. No roadmap.html involvement.
"""
import re
import pathlib

import pytest
import server

SERVER = pathlib.Path(__file__).resolve().parent.parent / "server.py"


def _py() -> str:
    return SERVER.read_text(encoding="utf-8", errors="replace")


def test_paste_listener_added_document_level():
    src = _py()
    assert "document.addEventListener('paste',function(e){" in src, "a document-level paste listener must exist (matching the drop handler)"


def test_paste_reuses_addfiles_no_second_upload_path():
    src = _py()
    m = re.search(r"document\.addEventListener\('paste',function\(e\)\{.*?\n\}\);", src, re.DOTALL)
    assert m, "paste handler not found"
    body = m.group(0)
    assert "addFiles(pf)" in body, "the paste handler must reuse addFiles, not duplicate the upload"
    # no second upload path inside the handler - it must NOT presign/upload on its own
    assert "/api/intake/" not in body, "the paste handler must not call the presign endpoint itself (addFiles does)"
    assert "FormData" not in body, "the paste handler must not build its own upload (addFiles does)"


def test_paste_extracts_files_ignores_text():
    src = _py()
    m = re.search(r"document\.addEventListener\('paste',function\(e\)\{.*?\n\}\);", src, re.DOTALL)
    assert m, "paste handler not found"
    body = m.group(0)
    assert "items[i].kind==='file'" in body, "only FILE entries are taken (text/'string' entries are ignored)"


def test_paste_preventdefault_only_when_files_found():
    src = _py()
    m = re.search(r"document\.addEventListener\('paste',function\(e\)\{.*?\n\}\);", src, re.DOTALL)
    assert m, "paste handler not found"
    body = m.group(0)
    # the guard against hijacking text paste: return before preventDefault when no file was found
    ret = body.find("if(!pf.length) return;")
    pd = body.find("e.preventDefault();")
    assert ret != -1 and pd != -1 and ret < pd, "preventDefault must fire only AFTER confirming a file was found (never for plain text)"


def test_drop_and_file_input_unchanged():
    src = _py()
    assert "document.addEventListener('drop',function(e){" in src, "drag-drop must still work"
    assert "$('#files').addEventListener('change',onFiles);" in src, "the file input must still work"


def test_no_endpoint_or_policy_change():
    src = _py()
    # the presign endpoint + its policy conditions are untouched by this stage
    assert 'generate_presigned_post(' in src, "the presign POST conversion must be intact"
    assert '["content-length-range", 0, _INTAKE_MAX_ATTACH_BYTES]' in src, "PRESIGN-CAP-1's size policy must be intact"


# ── Addendum A: the rate-limit message parameter (shared helper) ──────────────
def test_rate_limit_default_message_byte_identical():
    # the login limiter and every other caller must be UNCHANGED - default (no message) raises the
    # original login copy, byte for byte. This is the security-sensitive guard.
    server._rate.clear()
    for _ in range(server.RATE_MAX):
        server._check_rate_limit("k-default")
    with pytest.raises(server.HTTPException) as ei:
        server._check_rate_limit("k-default")
    assert ei.value.status_code == 429
    assert ei.value.detail == f"Too many login attempts. Try again in {server.RATE_WINDOW} seconds."


def test_rate_limit_custom_message_for_uploads():
    server._rate.clear()
    msg = "Too many uploads at once. Wait about a minute and try again."
    for _ in range(server.RATE_MAX):
        server._check_rate_limit("k-upload", msg)
    with pytest.raises(server.HTTPException) as ei:
        server._check_rate_limit("k-upload", msg)
    assert ei.value.status_code == 429 and ei.value.detail == msg


def test_limit_and_window_unchanged():
    # message-only change: the 10th still succeeds, the 11th still fails, status still 429
    server._rate.clear()
    assert (server.RATE_WINDOW, server.RATE_MAX) == (60, 10)
    for _ in range(server.RATE_MAX):
        server._check_rate_limit("k-limit")     # 10 succeed
    with pytest.raises(server.HTTPException) as ei:
        server._check_rate_limit("k-limit")     # 11th fails
    assert ei.value.status_code == 429


def test_only_intake_presign_passes_a_message():
    src = _py()
    assert "def _check_rate_limit(ip: str, message: str = None):" in src, "the optional message param must exist"
    assert 'raise HTTPException(429, message or f"Too many login attempts. Try again in {RATE_WINDOW} seconds.")' in src, \
        "the default must be the original login string byte-for-byte"
    assert '_check_rate_limit("intake-att:" + ip, "Too many uploads at once. Wait about a minute and try again.")' in src, \
        "the intake presign must pass the upload message"
    # NO other caller passes a second argument (a shared helper is where an unnoticed caller breaks)
    two_arg = [c for c in re.findall(r"_check_rate_limit\([^)]*,[^)]*\)", src) if "message: str" not in c]
    assert len(two_arg) == 1, f"only the intake presign may pass a message; found {len(two_arg)}: {two_arg}"
