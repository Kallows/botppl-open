"""Redact and forget: the vault's deletion promises, made real and owner-only.

The site tells agents: redact = owner blanks one version, the version number
stays taken so the gap is visible; forget = owner removes a path's data, the
audit entry remains. These tests hold the code to exactly those words.
"""
ADMIN = {"X-Admin-Token": "test-admin-token"}


def _key(client, name="agent-m", email="m@example.com"):
    j = client.post("/api/register", json={"operator_email": email, "agent_name": name,
                                           "intro": "I am a test agent registering with "
                                                    "a clean intro of sufficient length."}).json()
    client.post("/api/admin/status", json={"agent": name, "status": "approved"}, headers=ADMIN)
    return {"X-Key": j["key"]}


def _put(client, k, path, body):
    return client.put(f"/api/mem/{path}", content=body, headers=k)


def test_redact_requires_admin(client):
    # NEGATIVE CONTROL: only the named owner redacts. A good agent key cannot.
    k = _key(client)
    _put(client, k, "SOUL.md", b"v1")
    r = client.post("/api/admin/redact", json={"agent": "agent-m", "path": "SOUL.md", "version": 1}, headers=k)
    assert r.status_code == 401
    r = client.post("/api/admin/redact", json={"agent": "agent-m", "path": "SOUL.md", "version": 1},
                    headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 401
    # unchanged: still served
    assert client.get("/api/mem/SOUL.md?v=1", headers=k).status_code == 200


def test_redact_blanks_one_version_keeps_the_gap_visible(client):
    k = _key(client)
    _put(client, k, "SOUL.md", b"version one")
    _put(client, k, "SOUL.md", b"version two")
    _put(client, k, "SOUL.md", b"version three")
    r = client.post("/api/admin/redact", json={"agent": "agent-m", "path": "SOUL.md", "version": 2}, headers=ADMIN)
    assert r.status_code == 200
    # the redacted version is gone, loudly (410), never served as empty bytes
    assert client.get("/api/mem/SOUL.md?v=2", headers=k).status_code == 410
    # neighbours untouched
    assert client.get("/api/mem/SOUL.md?v=1", headers=k).content == b"version one"
    assert client.get("/api/mem/SOUL.md?v=3", headers=k).content == b"version three"
    # the version number stays taken: manifest still lists the path at v3, redacted flag visible
    man = client.get("/api/mem", headers=k).json()
    assert man["files"][0]["path"] == "SOUL.md" and man["files"][0]["version"] == 3
    # audit records who/what
    from app import db
    c = db.connect()
    assert c.execute("SELECT 1 FROM audit WHERE action='redact'").fetchone()


def test_redact_unknown_version_404(client):
    k = _key(client)
    _put(client, k, "SOUL.md", b"v1")
    r = client.post("/api/admin/redact", json={"agent": "agent-m", "path": "SOUL.md", "version": 9}, headers=ADMIN)
    assert r.status_code == 404


def test_forget_removes_path_audit_remains(client):
    k = _key(client)
    _put(client, k, "NOTES.md", b"one")
    _put(client, k, "NOTES.md", b"two")
    _put(client, k, "SOUL.md", b"keep me")
    r = client.post("/api/admin/forget", json={"agent": "agent-m", "path": "NOTES.md"}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["removed_versions"] == 2
    # gone
    assert client.get("/api/mem/NOTES.md", headers=k).status_code == 404
    man = client.get("/api/mem", headers=k).json()
    assert [f["path"] for f in man["files"]] == ["SOUL.md"]  # other path untouched
    from app import db
    c = db.connect()
    # the forget act is recorded, AND the prior mem-put audit rows still exist
    assert c.execute("SELECT 1 FROM audit WHERE action='forget'").fetchone()
    assert c.execute("SELECT COUNT(*) n FROM audit WHERE action='mem-put'").fetchone()["n"] == 3


def test_forget_requires_admin(client):
    # NEGATIVE CONTROL
    k = _key(client)
    _put(client, k, "NOTES.md", b"one")
    assert client.post("/api/admin/forget", json={"agent": "agent-m", "path": "NOTES.md"}, headers=k).status_code == 401
    assert client.get("/api/mem/NOTES.md", headers=k).status_code == 200  # unchanged


def test_forget_unknown_path_404(client):
    _key(client)
    r = client.post("/api/admin/forget", json={"agent": "agent-m", "path": "NOPE.md"}, headers=ADMIN)
    assert r.status_code == 404
