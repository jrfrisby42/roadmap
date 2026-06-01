"""
Shared pytest fixtures for the Frazil Roadmap backend test suite.

Test isolation strategy
-----------------------
`server.py` keeps each team in its own SQLite DB under ``TENANTS_DIR`` and runs
``boot()`` at *import* time. To keep tests from ever touching real data, we point
``FRAZIL_TENANTS_DIR`` at a throwaway temp directory and pin ``TOKEN_SECRET``
*before* importing ``server`` (the env reads happen at module-import time).

Each test gets a fresh, uniquely-named team so state never leaks between tests.
Role-gating / business-logic tests mint tokens directly via ``server.create_token``
to avoid the login rate limiter; the login flow itself is exercised in test_auth.
"""
import os
import sys
import atexit
import shutil
import tempfile

# ── Isolate BEFORE importing server (env is read at import time) ───────────────
_TMP_TENANTS = tempfile.mkdtemp(prefix="frazil_test_tenants_")
os.environ["FRAZIL_TENANTS_DIR"] = _TMP_TENANTS
os.environ["TOKEN_SECRET"] = "test-secret-do-not-use-in-prod"
# Neutralise Jira so no test can make a network call.
os.environ["JIRA_EMAIL"] = ""
os.environ["JIRA_API_TOKEN"] = ""

# Make the repo root importable (tests/ lives one level down).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import server  # noqa: E402  (must come after the env setup above)

atexit.register(lambda: shutil.rmtree(_TMP_TENANTS, ignore_errors=True))

_team_counter = {"n": 0}


@pytest.fixture(scope="session")
def client():
    """A TestClient bound to the FastAPI app (shared across the session)."""
    return TestClient(server.app)


@pytest.fixture
def team():
    """Create a fresh, uniquely-named team and return its slug.

    The team's admin user is the default ``admin`` / ``frazil123``.
    Also resets the in-memory login rate limiter so per-test login attempts
    start from a clean slate (the limiter is keyed by client IP, shared across
    the TestClient).
    """
    _team_counter["n"] += 1
    slug = f"team{_team_counter['n']}"
    os.makedirs(os.path.join(server.TENANTS_DIR, slug), exist_ok=True)
    server.init_team_db(slug)
    server._rate.clear()
    return slug


def _headers(team_slug, username, role):
    token = server.create_token(team_slug, username, role)
    return {"Authorization": f"Bearer {token}", "X-Team": team_slug}


@pytest.fixture
def admin_headers(team):
    return _headers(team, "admin", "admin")


@pytest.fixture
def editor_headers(team):
    return _headers(team, "editor1", "editor")


@pytest.fixture
def viewer_headers(team):
    return _headers(team, "viewer1", "viewer")
