"""WATCH-BATCH-1 - atomic bulk watch/unwatch.

POST /api/items/unwatch-batch and /api/items/watch-batch each act in ONE transaction (all or none),
scope to the caller's own watcher rows (username from the token), match the single-item endpoints'
read enforcement (watch gates on read scope, unwatch does not), cap the list at _WATCH_BATCH_MAX
(422 above), and return the ids that actually changed so the client's undo is exact, not hopeful.
"""
import contextlib
import sqlite3

import pytest

import server


def _hdr(team, user, role="editor"):
    return {"Authorization": f"Bearer {server.create_token(team, user, role)}", "X-Team": team}


def _mk(client, headers):
    return client.post("/api/projects", json={"name": "Item", "status": "New"}, headers=headers).json()["id"]


def _watches(team, pid, username):
    with server.db(team) as c:
        return c.execute("SELECT 1 FROM watchers WHERE item_id=? AND username=?", (pid, username)).fetchone() is not None


def _clear_watchers(team):
    with server.db(team) as c:
        c.execute("DELETE FROM watchers")


def test_unwatch_batch_removes_and_returns_removed(client, team, admin_headers):
    pids = [_mk(client, admin_headers) for _ in range(3)]        # create auto-watches admin
    r = client.post("/api/items/unwatch-batch", json={"ids": pids}, headers=admin_headers)
    assert r.status_code == 200
    assert sorted(r.json()["removed"]) == sorted(pids)           # returns what it removed
    for p in pids:
        assert not _watches(team, p, "admin")


def test_watch_batch_adds_and_returns_watched(client, team, admin_headers):
    pids = [_mk(client, admin_headers) for _ in range(3)]
    _clear_watchers(team)
    r = client.post("/api/items/watch-batch", json={"ids": pids}, headers=admin_headers)
    assert r.status_code == 200
    assert sorted(r.json()["watched"]) == sorted(pids)
    for p in pids:
        assert _watches(team, p, "admin")


def test_undo_symmetry_uses_removed_list(client, team, admin_headers):
    pids = [_mk(client, admin_headers) for _ in range(3)]
    removed = client.post("/api/items/unwatch-batch", json={"ids": pids}, headers=admin_headers).json()["removed"]
    # the client feeds the server's removed list back to watch-batch (the undo)
    client.post("/api/items/watch-batch", json={"ids": removed}, headers=admin_headers)
    for p in pids:
        assert _watches(team, p, "admin")


def test_unwatch_batch_only_removes_own(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    server._add_watchers(team, pid, ["bob"])                     # a second watcher
    client.post("/api/items/unwatch-batch", json={"ids": [pid]}, headers=admin_headers)
    assert not _watches(team, pid, "admin")                      # caller gone
    assert _watches(team, pid, "bob")                            # other watcher survives


def test_unwatch_batch_idempotent(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    _clear_watchers(team)                                        # admin does not watch it
    r = client.post("/api/items/unwatch-batch", json={"ids": [pid, 999999]}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["removed"] == []    # nothing watched -> nothing removed, no error


def test_username_from_token_not_body(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    server._add_watchers(team, pid, ["bob"])
    # a body-supplied username must be ignored - only the token's user (admin) is affected
    client.post("/api/items/unwatch-batch", json={"ids": [pid], "username": "bob"}, headers=admin_headers)
    assert _watches(team, pid, "bob")                            # bob untouched by the crafted body
    assert not _watches(team, pid, "admin")                      # only the token user was unwatched


def test_batch_cap_422(client, team, admin_headers):
    over = list(range(server._WATCH_BATCH_MAX + 1))
    r = client.post("/api/items/unwatch-batch", json={"ids": over}, headers=admin_headers)
    assert r.status_code == 422
    r2 = client.post("/api/items/watch-batch", json={"ids": over}, headers=admin_headers)
    assert r2.status_code == 422


def test_watch_batch_atomic_rollback(client, team, admin_headers, monkeypatch):
    # The central claim: a forced mid-batch failure watches NOTHING (all or none). A proxy raises on the
    # 3rd INSERT; the with-db helper rolls back the first two, so none of the four end up watched.
    pids = [_mk(client, admin_headers) for _ in range(4)]
    _clear_watchers(team)
    orig_db = server.db

    class _Proxy:
        def __init__(self, c): self._c = c; self._n = 0
        def execute(self, sql, *a, **k):
            if sql.lstrip().upper().startswith("INSERT"):
                self._n += 1
                if self._n == 3:
                    raise sqlite3.OperationalError("forced mid-batch failure")
            return self._c.execute(sql, *a, **k)
        def __getattr__(self, name): return getattr(self._c, name)

    @contextlib.contextmanager
    def _failing_db(t):
        with orig_db(t) as c:
            yield _Proxy(c)

    monkeypatch.setattr(server, "db", _failing_db)
    with pytest.raises(sqlite3.OperationalError):               # the batch fails mid-loop (TestClient re-raises the 500)
        client.post("/api/items/watch-batch", json={"ids": pids}, headers=admin_headers)
    monkeypatch.undo()
    for p in pids:
        assert not _watches(team, p, "admin")                   # watched NOTHING - the first two INSERTs rolled back
