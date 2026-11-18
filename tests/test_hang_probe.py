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
