"""X-Latest: every 200 from GET /api/mem/<path> carries the newest version
number for that agent+path, so a pinned read (?v=N) can tell it is behind.

Scoping rules held exactly: scoped to the calling agent (other agents'
versions never leak) and to the path. MAX includes redacted versions — the
version number stays taken, the gap stays visible. Error responses (404,
409, 410) are unchanged and carry no X-Latest.
"""
from tests.integration.test_api import make_approved


def _put(client, k, path, body):
    return client.put(f"/api/mem/{path}", content=body, headers=k)


def _get(client, k, path, v=None):
    url = f"/api/mem/{path}" + (f"?v={v}" if v is not None else "")
    return client.get(url, headers=k)


# Case 1: agent writes SOUL.md three times; GET ?v=1 -> 200, X-Version "1", X-Latest "3"
def test_x_latest_pinned_read(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    for i in (1, 2, 3):
        _put(client, k, "SOUL.md", f"v{i}".encode())
    r = _get(client, k, "SOUL.md", v=1)
    assert r.status_code == 200
    assert r.headers["X-Version"] == "1"
    assert r.headers["X-Latest"] == "3"
    assert r.headers["X-SHA256"]


# Case 2: same; GET without v -> 200, X-Version "3", X-Latest "3"
def test_x_latest_unpinned_read(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    for i in (1, 2, 3):
        _put(client, k, "SOUL.md", f"v{i}".encode())
    r = _get(client, k, "SOUL.md")
    assert r.status_code == 200
    assert r.headers["X-Version"] == "3"
    assert r.headers["X-Latest"] == "3"


# Case 3: v1..v3, admin redacts v3; GET ?v=2 -> 200, X-Version "2", X-Latest "3"
def test_x_latest_counts_redacted(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    for i in (1, 2, 3):
        _put(client, k, "SOUL.md", f"v{i}".encode())
    r = client.post("/api/admin/redact",
                    json={"agent": "agent-a", "path": "SOUL.md", "version": 3},
                    headers={"X-Admin-Token": "test-admin-token"})
    assert r.status_code == 200
    r = _get(client, k, "SOUL.md", v=2)
    assert r.status_code == 200
    assert r.headers["X-Version"] == "2"
    assert r.headers["X-Latest"] == "3"


# Case 4: same as 3; GET ?v=3 -> 410 (unchanged behaviour)
def test_redacted_read_still_410(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    for i in (1, 2, 3):
        _put(client, k, "SOUL.md", f"v{i}".encode())
    client.post("/api/admin/redact",
                json={"agent": "agent-a", "path": "SOUL.md", "version": 3},
                headers={"X-Admin-Token": "test-admin-token"})
    r = _get(client, k, "SOUL.md", v=3)
    assert r.status_code == 410
    assert "X-Latest" not in r.headers


# Case 5: agent A writes SOUL.md x3, agent B writes SOUL.md x1; B GETs SOUL.md
# -> X-Latest "1" (scoped to the caller: A's versions never leak)
def test_x_latest_scoped_to_caller(client):
    ka = make_approved(client, "agent-a", "op-a@example.com")
    kb = make_approved(client, "agent-b", "op-b@example.com")
    for i in (1, 2, 3):
        _put(client, ka, "SOUL.md", f"a{i}".encode())
    _put(client, kb, "SOUL.md", b"b1")
    r = _get(client, kb, "SOUL.md")
    assert r.status_code == 200
    assert r.headers["X-Version"] == "1"
    assert r.headers["X-Latest"] == "1"


# Case 6: two paths, a.md x2 and b.md x5; GET a.md?v=1 -> X-Latest "2"
# (scoped to the path)
def test_x_latest_scoped_to_path(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    for i in (1, 2):
        _put(client, k, "a.md", f"a{i}".encode())
    for i in (1, 2, 3, 4, 5):
        _put(client, k, "b.md", f"b{i}".encode())
    r = _get(client, k, "a.md", v=1)
    assert r.status_code == 200
    assert r.headers["X-Version"] == "1"
    assert r.headers["X-Latest"] == "2"


# Case 7: GET ?v=9 on a path with 3 versions -> 404, unchanged
def test_unknown_version_still_404(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    for i in (1, 2, 3):
        _put(client, k, "SOUL.md", f"v{i}".encode())
    r = _get(client, k, "SOUL.md", v=9)
    assert r.status_code == 404
    assert "X-Latest" not in r.headers
