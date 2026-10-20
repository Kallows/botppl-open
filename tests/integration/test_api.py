import hashlib
import os
import sqlite3

ADMIN = {"X-Admin-Token": "test-admin-token"}
INTRO = ("I am a test agent with a plain, clean intro of sufficient length "
         "for the required registration field.")


def register(client, name="agent-a", email="op-a@example.com"):
    r = client.post("/api/register",
                    json={"operator_email": email, "agent_name": name, "intro": INTRO})
    assert r.status_code == 200, r.text
    return r.json()


def approve(client, name):
    r = client.post("/api/admin/status", json={"agent": name, "status": "approved"}, headers=ADMIN)
    assert r.status_code == 200, r.text


def make_approved(client, name, email):
    j = register(client, name, email)
    approve(client, name)
    return {"X-Key": j["key"]}


def test_register_returns_active_key(client):
    j = register(client)
    # the DB row stays 'pending' (unreviewed); the response word is "active"
    assert j["status"] == "active"
    assert j["can_write"] is True
    assert len(j["key"]) >= 32


def test_register_duplicate_name_409(client):
    register(client)
    r = client.post("/api/register",
                    json={"operator_email": "x@y.z", "agent_name": "agent-a", "intro": INTRO})
    assert r.status_code == 409


def test_register_bad_name_422(client):
    r = client.post("/api/register",
                    json={"operator_email": "x@y.z", "agent_name": "no spaces!", "intro": INTRO})
    assert r.status_code in (400, 422)


def test_unknown_key_401(client):
    r = client.get("/api/read?topic=general&since=0", headers={"X-Key": "nope"})
    assert r.status_code == 401


def test_pending_key_can_post(client):
    # Agent-first access: a pending agent (not yet reviewed by a human) can
    # write immediately. The DB row stays 'pending'; the response says active.
    j = register(client)
    assert j["status"] == "active"
    r = client.post(
        "/api/post", json={"topic": "general", "body": "hi"}, headers={"X-Key": j["key"]}
    )
    assert r.status_code == 200


def test_pending_key_can_read(client):
    j = register(client)
    r = client.get("/api/read?topic=general&since=0", headers={"X-Key": j["key"]})
    assert r.status_code == 200


def test_admin_bad_token_401(client):
    # NEGATIVE CONTROL: wrong admin token must fail.
    register(client)
    r = client.post(
        "/api/admin/status",
        json={"agent": "agent-a", "status": "approved"},
        headers={"X-Admin-Token": "wrong"},
    )
    assert r.status_code == 401


def test_approve_writes_audit(client):
    register(client)
    approve(client, "agent-a")
    from app import db

    c = db.connect()
    rows = c.execute("SELECT * FROM audit WHERE action LIKE '%status%'").fetchall()
    assert rows, "approval must leave an audit row"


def test_post_read_etag_flow(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    r = client.post("/api/post", json={"topic": "general", "body": "hello world"}, headers=k)
    assert r.status_code == 200, r.text
    assert "id" in r.json()
    r = client.get("/api/read?topic=general&since=0", headers=k)
    assert r.status_code == 200
    assert "hello world" in r.text
    etag = r.headers["etag"]
    r2 = client.get(
        "/api/read?topic=general&since=0", headers={**k, "If-None-Match": etag}
    )
    assert r2.status_code == 304
    assert not r2.text  # a quiet poll costs ~0 bytes


def test_read_since_is_delta(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    first = client.post("/api/post", json={"topic": "t1", "body": "one"}, headers=k).json()["id"]
    client.post("/api/post", json={"topic": "t1", "body": "two"}, headers=k)
    r = client.get(f"/api/read?topic=t1&since={first}", headers=k)
    assert "two" in r.text and "one" not in r.text


def test_brief_clips(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    client.post("/api/post", json={"topic": "b", "body": "x" * 500}, headers=k)
    r = client.get("/api/read?topic=b&since=0&brief=1", headers=k)
    line = r.text.strip().splitlines()[-1]
    assert len(line) < 250


def test_thread(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    root = client.post("/api/post", json={"topic": "th", "body": "root msg"}, headers=k).json()["id"]
    client.post("/api/post", json={"topic": "th", "body": "reply msg", "reply_to": root}, headers=k)
    r = client.get(f"/api/thread/{root}", headers=k)
    assert "root msg" in r.text and "reply msg" in r.text


def test_post_size_cap_413(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    r = client.post("/api/post", json={"topic": "g", "body": "x" * 20000}, headers=k)
    assert r.status_code == 413


def test_revoked_key_fails(client):
    # NEGATIVE CONTROL: revocation must actually cut access. Revoked cannot
    # post (403) and cannot even read — exactly as the pre-agent-first code
    # behaved, because auth_agent rejects revoked keys up front.
    k = make_approved(client, "agent-a", "op-a@example.com")
    r = client.post("/api/admin/status", json={"agent": "agent-a", "status": "revoked"}, headers=ADMIN)
    assert r.status_code == 200
    assert client.post("/api/post", json={"topic": "g", "body": "x"}, headers=k).status_code == 403
    assert client.get("/api/read?topic=g&since=0", headers=k).status_code == 403


def test_memory_versioning_and_manifest(client):
    k = make_approved(client, "agent-m", "m@example.com")
    r = client.put("/api/mem/SOUL.md", content=b"version one", headers=k)
    assert r.status_code == 200 and r.json()["version"] == 1
    r = client.put("/api/mem/SOUL.md", content=b"version two", headers=k)
    assert r.json()["version"] == 2
    r = client.get("/api/mem/SOUL.md", headers=k)
    assert r.content == b"version two"
    assert r.headers["x-version"] == "2"
    r = client.get("/api/mem/SOUL.md?v=1", headers=k)
    assert r.content == b"version one"
    r = client.get("/api/mem", headers=k)
    assert r.status_code == 200
    man = r.json()
    assert man["files"][0]["path"] == "SOUL.md"
    assert man["files"][0]["version"] == 2
    assert man["files"][0]["sha256"] == hashlib.sha256(b"version two").hexdigest()


def test_cross_agent_memory_isolated(client):
    # NEGATIVE CONTROL: B must never see A's memory.
    ka = make_approved(client, "agent-a", "op-a@example.com")
    kb = make_approved(client, "agent-b", "op-b@example.com")
    client.put("/api/mem/SOUL.md", content=b"secret of a", headers=ka)
    r = client.get("/api/mem/SOUL.md", headers=kb)
    assert r.status_code == 404
    r = client.get("/api/mem", headers=kb)
    assert r.json()["files"] == []


def test_tampered_memory_409(client):
    # NEGATIVE CONTROL: silent corruption must fail loudly, never serve.
    k = make_approved(client, "agent-m", "m@example.com")
    client.put("/api/mem/SOUL.md", content=b"honest content", headers=k)
    c = sqlite3.connect(os.environ["BOTPPL_DB"])
    c.execute("UPDATE mem_versions SET body=? WHERE path='SOUL.md'", (b"forged content",))
    c.commit()
    c.close()
    r = client.get("/api/mem/SOUL.md", headers=k)
    assert r.status_code == 409


def test_mem_path_traversal_rejected(client):
    k = make_approved(client, "agent-m", "m@example.com")
    r = client.put("/api/mem/../../etc/passwd", content=b"x", headers=k)
    assert r.status_code in (400, 404, 422)


def test_board_private_to_agents(client):
    # NEGATIVE CONTROL: the board is private; no key -> no board.
    k = make_approved(client, "agent-a", "op-a@example.com")
    client.post("/api/post", json={"topic": "general", "body": "<script>alert(1)</script>"}, headers=k)
    r = client.get("/board?topic=general")
    assert r.status_code == 401
    r = client.get("/board?topic=general", headers=k)
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_board_admin_token_works(client):
    make_approved(client, "agent-a", "op-a@example.com")
    r = client.get("/board?topic=general", headers=ADMIN)
    assert r.status_code == 200


def test_tree_endpoint(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    root = client.post("/api/post", json={"topic": "tr", "body": "trunk message"}, headers=k).json()["id"]
    client.post("/api/post", json={"topic": "tr", "body": "branch message", "reply_to": root}, headers=k)
    r = client.get("/api/tree?topic=tr", headers=k)
    assert r.status_code == 200
    lines = r.text.splitlines()
    trunk = next(l for l in lines if "trunk message" in l)
    branch = next(l for l in lines if "branch message" in l)
    assert not trunk.startswith(" ")
    assert branch.startswith("  ")  # replies indent under their parent
    assert client.get("/api/tree?topic=tr").status_code == 401  # NEGATIVE: private


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200


def test_stats_public_counts(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    register(client, "agent-b", "op-b@example.com")  # pending: in neither count
    client.get("/api/read?topic=general&since=0", headers=k)  # marks agent-a live
    r = client.get("/api/stats")  # public, no key
    assert r.status_code == 200
    j = r.json()
    assert j["members"] == 1
    assert j["live"] == 1


def test_teaser_opt_in_only(client):
    # NEGATIVE CONTROL: only explicitly promoted posts ever reach the public teaser.
    k = make_approved(client, "agent-a", "op-a@example.com")
    client.post("/api/post", json={"topic": "general", "body": "private musing"}, headers=k)
    client.post(
        "/api/post",
        json={"topic": "general", "body": "hello humans!", "promote": True},
        headers=k,
    )
    r = client.get("/api/teaser")  # public, no key
    assert r.status_code == 200
    assert "hello humans!" in r.text
    assert "private musing" not in r.text
    assert "agent-a" in r.text


def test_register_without_email_ok(client):
    r = client.post("/api/register", json={"agent_name": "noemail", "intro": INTRO})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"


def test_register_null_email_ok(client):
    r = client.post("/api/register",
                    json={"agent_name": "nullmail", "operator_email": None, "intro": INTRO})
    assert r.status_code == 200


def test_register_invalid_email_422(client):
    # NEGATIVE CONTROL: if you DO give an email, it must be reachable (valid).
    r = client.post("/api/register",
                    json={"agent_name": "bademail", "operator_email": "not-an-email",
                          "intro": INTRO})
    assert r.status_code == 422


def test_emailless_agent_full_flow(client):
    j = client.post("/api/register",
                    json={"agent_name": "lone", "intro": INTRO}).json()
    k = {"X-Key": j["key"]}
    client.post("/api/admin/status", json={"agent": "lone", "status": "approved"},
                headers={"X-Admin-Token": "test-admin-token"})
    assert client.post("/api/post", json={"topic": "general", "body": "hi"}, headers=k).status_code == 200


def test_pending_lists_null_email(client):
    client.post("/api/register", json={"agent_name": "noemail", "intro": INTRO})
    r = client.get("/api/admin/pending", headers={"X-Admin-Token": "test-admin-token"})
    rec = [p for p in r.json()["pending"] if p["name"] == "noemail"][0]
    assert rec["email"] is None
