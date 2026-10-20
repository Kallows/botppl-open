"""Every GET route also answers HEAD: same status and headers as GET, empty
body at the client. HEAD must never be routed into a POST-only handler and
must never write anything a GET would not."""
import sqlite3

INTRO = ("I am a test agent with a plain, clean intro of sufficient length "
         "for the required registration field.")


def register(client, name="agent-a", email="op-a@example.com"):
    r = client.post("/api/register",
                    json={"operator_email": email, "agent_name": name, "intro": INTRO})
    assert r.status_code == 200, r.text
    return r.json()


def _counts(path):
    c = sqlite3.connect(path)
    try:
        audit_rows = c.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        messages = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return audit_rows, messages
    finally:
        c.close()


def test_head_healthz(client):
    r = client.head("/healthz")
    assert r.status_code == 200
    assert len(r.content) == 0


def test_head_stats_matches_get(client):
    g = client.get("/api/stats")
    h = client.head("/api/stats")
    assert g.status_code == 200
    assert h.status_code == g.status_code
    assert len(h.content) == 0
    assert h.headers["content-type"] == g.headers["content-type"]


def test_head_read_etag_matches_get(client):
    j = register(client)
    k = {"X-Key": j["key"]}
    params = "/api/read?topic=general&since=0"
    g = client.get(params, headers=k)
    assert g.status_code == 200
    h = client.head(params, headers=k)
    assert h.status_code == 200
    assert h.headers["etag"] == g.headers["etag"]
    assert len(h.content) == 0


def test_head_read_no_key_not_405(client):
    # NEGATIVE CONTROL: HEAD must hit the same auth as GET, not 405.
    g = client.get("/api/read?topic=general&since=0")
    h = client.head("/api/read?topic=general&since=0")
    assert g.status_code != 405
    assert h.status_code == g.status_code


def test_head_mem_matches_get(client):
    j = register(client)
    k = {"X-Key": j["key"]}
    r = client.put("/api/mem/notes/todo", content=b"buy milk", headers=k)
    assert r.status_code == 200, r.text
    g = client.get("/api/mem/notes/todo", headers=k)
    h = client.head("/api/mem/notes/todo", headers=k)
    assert g.status_code == 200
    assert h.status_code == 200
    assert h.headers["x-version"] == g.headers["x-version"]
    assert h.headers["x-sha256"] == g.headers["x-sha256"]
    assert len(h.content) == 0


def test_head_post_only_route_405(client):
    # NEGATIVE CONTROL: HEAD must never be routed into a POST handler.
    r = client.head("/api/register")
    assert r.status_code == 405


def test_head_read_writes_nothing(client, env):
    # NEGATIVE CONTROL: HEAD on /api/read must leave the same row counts
    # as GET -- no extra audit rows, no extra messages.
    import os

    j = register(client)
    k = {"X-Key": j["key"]}
    params = "/api/read?topic=general&since=0"
    path = os.environ["BOTPPL_DB"]
    client.get(params, headers=k)
    before_get = _counts(path)
    client.get(params, headers=k)
    after_get = _counts(path)
    before_head = _counts(path)
    client.head(params, headers=k)
    after_head = _counts(path)
    assert after_get == before_get
    assert after_head == before_head
