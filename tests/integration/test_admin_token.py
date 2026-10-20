"""F9 admin token comparison.

secrets.compare_digest raises TypeError on a non-ASCII str (-> 500). The
comparison is on UTF-8 encoded bytes, so any token str is 401, never 500.

Case table (task #16):
  X-Admin-Token: "\\xe9" on /api/admin/pending | 401, not 500
  preserve: right token 200, wrong ASCII token 401
"""
from fastapi.testclient import TestClient

ADMIN = {"X-Admin-Token": "test-admin-token"}


def test_non_ascii_token_compare_is_401_not_500(env):
    # The exact str from the case row, straight at the compare: the old
    # str-vs-str compare_digest raised TypeError here (500 on the wire).
    from app.main import _admin_token_ok

    assert _admin_token_ok("\xe9") is False, "non-ASCII token -> 401 path, never TypeError"


def test_non_ascii_token_on_endpoint_401(env):
    # Wire level: any non-ASCII header value must come back 401, never 500.
    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/api/admin/pending", headers={"X-Admin-Token": b"\xc3\xa9"})
    assert r.status_code == 401, r.text
    assert r.status_code != 500


def test_admin_token_ascii_paths(client):
    r = client.get("/api/admin/pending", headers=ADMIN)
    assert r.status_code == 200
    r = client.get("/api/admin/pending", headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 401
