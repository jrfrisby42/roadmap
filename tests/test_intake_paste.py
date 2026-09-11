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
