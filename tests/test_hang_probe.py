"""HANG-PROBE-1: a load that froze reports its start-up trace on the next load. Server: /api/client-log only logs
(auth required, JSON object only). Client: source guards for the trace + where it is reported."""
import logging
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "roadmap.html").read_text(encoding="utf-8", errors="replace")


def test_client_log_needs_auth(client):
    assert client.post("/api/client-log", json={"kind": "boot-hang"}).status_code == 401


def test_client_log_logs_the_trace(client, admin_headers, caplog):
    with caplog.at_level(logging.WARNING, logger="frazil"):
        r = client.post("/api/client-log", json={"kind": "boot-hang", "trace": {"steps": ["page@0", "data@900"]}}, headers=admin_headers)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert any("[client-log] boot-hang" in m and "data@900" in m for m in caplog.messages)


def test_client_log_rejects_non_objects(client, admin_headers):
    assert client.post("/api/client-log", content=b"not json", headers=admin_headers).status_code == 422
    assert client.post("/api/client-log", json=[1, 2], headers=admin_headers).status_code == 422


def test_page_traces_start_up_and_reports_an_unsettled_load():
    assert "var KEY = 'frazil_boot_trace'" in HTML
    assert "indexOf('data@') === 0" in HTML                                     # only signed-in loads count
    assert "window.addEventListener('pagehide', function(){ window._frzMark('left', true); });" in HTML
    assert "API.post('/api/client-log', { kind: 'boot-hang', trace: window._frzPrevBootTrace })" in HTML
    assert "window._frzMark && window._frzMark('settled', true); }, 10000);" in HTML
    for step in ("'script-b'", "'script-c'", "'boot'", "'data'", "'build'", "'wire'", "'rail'", "'shown'", "'caps'", "'acts'", "'sw-reg'", "'sw-claim'"):
        assert step in HTML, step


def test_page_samples_the_stack_while_starting_up():
    # HANG-PROBE-2: hot helpers + Date stepping + the shell's _call/_val sample the stack until the page settles
    assert "window._frzSample = function(){" in HTML and "tr.stack = String(new Error().stack || '')" in HTML
    assert "['setDate', 'toISOString', 'getDay'].forEach(function(n){ window._frzHook(Date.prototype, n); });" in HTML
    assert "if(done){ tr.done = true; settled = true; try { window._frzUnsample(); } catch(_e){} }" in HTML
    assert "setTimeout(function(){ stopS = true; window._frzUnsample(); }, 60000);" in HTML
    assert "function _call(name){ if(window._frzSample) window._frzSample(); try {" in HTML
    assert "function _val(name, dflt){ if(window._frzSample) window._frzSample(); try {" in HTML
    assert "window._frzHook && window._frzHook(window, n); }); } catch(_e){}\nboot();" in HTML
