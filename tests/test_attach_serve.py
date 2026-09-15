"""ATTACH-SERVE-2: the attachment serve path must not let a stored script-capable type execute.

stream_attachment used to serve the stored contentType with Content-Disposition: inline, so a stored
text/html (or image/svg+xml) became a same-origin text/html blob on window.open and ran script in the
app origin. Fix: only an EXPLICITLY enumerated inline-safe set is served with its stored type inline;
everything else is coerced to application/octet-stream + attachment, and nosniff is always set. The
client mirrors the decision via the server-provided `inlineSafe` flag.

Real S3 needs live creds; _s3_client is a capturing/streaming fake (same boundary as test_attach_url).
"""
import json
import re
import pathlib

import server

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"
HTML = ROOT / "roadmap.html"


def _py():
    return SERVER.read_text(encoding="utf-8", errors="replace")


def _html():
    return HTML.read_text(encoding="utf-8", errors="replace")


class _FakeS3:
    def get_object(self, Bucket, Key):
        class _Body:
            def iter_chunks(self, chunk_size=65536):
                yield b"<x>bytes</x>"
        return {"Body": _Body(), "ContentType": "application/octet-stream", "ContentLength": 12}


def _item_with_att(client, team, admin_headers, ctype):
    """Create an item and inject one attachment record with the given stored contentType."""
    pid = client.post("/api/projects", json={"name": "att-host", "status": "New"},
                      headers=admin_headers).json()["id"]
    with server.db(team) as c:
        row = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
        p = json.loads(row["data"])
        rec = {"id": "at1", "key": f"items/{pid}/at1/f", "name": "f", "size": 12}
        if ctype is not _NO_CTYPE:
            rec["contentType"] = ctype
        p["attachments"] = [rec]
        c.execute("UPDATE projects SET data=? WHERE id=?", (json.dumps(p), pid))
    return pid


_NO_CTYPE = object()


def _serve(client, team, admin_headers, ctype, monkeypatch):
    monkeypatch.setattr(server, "_s3_client", lambda: _FakeS3())
    pid = _item_with_att(client, team, admin_headers, ctype)
    return client.get(f"/api/items/{pid}/attachments/at1/raw", headers=admin_headers)


def _coerced(r):
    return (r.headers.get("content-type", "").startswith("application/octet-stream")
            and r.headers.get("content-disposition", "").startswith("attachment"))


# ── case 1: text/html coerced ──────────────────────────────────────────────────
def test_html_served_as_octet_attachment(client, team, admin_headers, monkeypatch):
    r = _serve(client, team, admin_headers, "text/html", monkeypatch)
    assert r.status_code == 200
    assert _coerced(r), (r.headers.get("content-type"), r.headers.get("content-disposition"))


# ── case 2: image/svg+xml coerced (its OWN test - the glob-allowlist miss) ──────
def test_svg_served_as_octet_attachment(client, team, admin_headers, monkeypatch):
    r = _serve(client, team, admin_headers, "image/svg+xml", monkeypatch)
    assert r.status_code == 200
    assert _coerced(r), "image/svg+xml must NOT be inline (it executes as a document)"


# ── case 3: charset parameter stripped before the check ─────────────────────────
def test_html_with_charset_is_coerced(client, team, admin_headers, monkeypatch):
    r = _serve(client, team, admin_headers, "text/html; charset=utf-8", monkeypatch)
    assert _coerced(r), "a ;charset= parameter must not slip past the safe-list check"


# ── case 4: case-insensitive ────────────────────────────────────────────────────
def test_mixed_case_html_is_coerced(client, team, admin_headers, monkeypatch):
    r = _serve(client, team, admin_headers, "TEXT/HTML", monkeypatch)
    assert _coerced(r), "the safe-list check must be case-insensitive"


# ── case 5 (INVARIANT): image/png unchanged - stored type, inline ──────────────
def test_png_unchanged_inline(client, team, admin_headers, monkeypatch):
    r = _serve(client, team, admin_headers, "image/png", monkeypatch)
    assert r.headers.get("content-type", "").startswith("image/png")
    assert r.headers.get("content-disposition", "").startswith("inline")


# ── case 6 (INVARIANT): application/pdf unchanged - inline ─────────────────────
def test_pdf_unchanged_inline(client, team, admin_headers, monkeypatch):
    r = _serve(client, team, admin_headers, "application/pdf", monkeypatch)
    assert r.headers.get("content-type", "").startswith("application/pdf")
    assert r.headers.get("content-disposition", "").startswith("inline")


# ── case 7: missing/empty contentType fails closed (coerced, not inline) ───────
def test_missing_ctype_fails_closed(client, team, admin_headers, monkeypatch):
    r = _serve(client, team, admin_headers, _NO_CTYPE, monkeypatch)
    assert _coerced(r), "a missing contentType must be treated as UNSAFE (fail closed)"
    r2 = _serve(client, team, admin_headers, "", monkeypatch)
    assert _coerced(r2), "an empty contentType must be treated as UNSAFE"


# ── case 8: nosniff present ─────────────────────────────────────────────────────
def test_nosniff_header_present(client, team, admin_headers, monkeypatch):
    r = _serve(client, team, admin_headers, "text/html", monkeypatch)
    assert r.headers.get("x-content-type-options", "").lower() == "nosniff"
    r2 = _serve(client, team, admin_headers, "image/png", monkeypatch)
    assert r2.headers.get("x-content-type-options", "").lower() == "nosniff", "nosniff on safe responses too"


# ── the inlineSafe flag list_attachments feeds the client (server side of cases 9/10) ──
def test_list_attachments_inline_safe_flag(client, team, admin_headers, monkeypatch):
    monkeypatch.setattr(server, "_s3_client", lambda: _FakeS3())
    for ctype, expect in [("text/html", False), ("image/svg+xml", False), ("image/png", True),
                          ("application/pdf", True), ("text/html; charset=utf-8", False), (_NO_CTYPE, False)]:
        pid = _item_with_att(client, team, admin_headers, ctype)
        atts = client.get(f"/api/items/{pid}/attachments", headers=admin_headers).json()["attachments"]
        assert atts[0]["inlineSafe"] is expect, (ctype, atts[0]["inlineSafe"])


# ── case 9 (client): non-safe force-downloads, does NOT window.open the blob ────
def test_client_non_safe_downloads(  ):
    src = _html()
    # the open handler branches on the server flag and downloads (anchor + download=) when not safe
    assert "data-inlinesafe" in src, "the open element must carry the server inlineSafe flag"
    assert re.search(r"var safe=el\.getAttribute\('data-inlinesafe'\)==='1'", src), "handler reads the flag"
    assert re.search(r"if\(safe\)\{ window\.open\(b, '_blank', 'noopener'\); \}", src), "safe still opens a tab"
    assert "an.download=nm" in src, "non-safe must force a download, not window.open"


# ── case 10 (client, INVARIANT): a safe type still opens in a new tab ──────────
def test_client_safe_still_opens_tab():
    src = _html()
    # only a server-inline-safe image gets a thumbnail; the open still window.open's for safe
    assert "/^image\\//.test(a.contentType||'') && a.inlineSafe" in src, \
        "thumbnails gated on inlineSafe so an SVG doesn't render/open as a document"


# ── case 11 (INVARIANT): upload / presign / promote / intake untouched ─────────
def test_invariant_upload_and_intake_untouched():
    src = _py()
    # the authenticated presign still accepts any type (no allow-list added in its body) - Decision 7 stands
    m = re.search(r"def presign_attachment\(.*?\n@app\.", src, re.DOTALL).group(0)
    assert "_INLINE_SAFE_TYPES" not in m, "the fix must not touch the upload/presign path"
    # the public intake allow-list is unchanged and still excludes SVG/HTML
    assert '_INTAKE_ATTACH_TYPES = {"image/png"' in src, "the public intake allow-list is unchanged"
    intake_block = src.split("_INTAKE_ATTACH_TYPES")[1].split("}")[0]
    assert "image/svg" not in intake_block and "text/html" not in intake_block, "intake still excludes SVG/HTML"
    # the promote helper is untouched (no safe-list logic leaked into it)
    prom = re.search(r"def _promote_intake_attachments\(.*?\n@app\.", src, re.DOTALL).group(0)
    assert "_INLINE_SAFE_TYPES" not in prom, "promote path untouched"
    # the safe list is the single server source of truth, used by BOTH serve + list
    assert "_INLINE_SAFE_TYPES = frozenset(" in src
    assert src.count("_attach_is_inline_safe(") >= 2, "used by stream_attachment AND list_attachments"