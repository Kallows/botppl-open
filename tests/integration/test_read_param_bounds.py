"""F10/F11 edge 500s on the read paths, and F16 ETag over since/limit/brief.

F10: topic on the reads is validated with the same pattern the write path
uses (PostIn.topic) -> 422; "é" used to reach the ETag header (not latin-1)
and 500. F11: since, mid, v, limit and reply_to are bounded (ge=0, le=2**63-1)
-> 422 instead of sqlite OverflowError -> 500. F16: the ETag covers since,
limit and brief, so a stale ETag can never 304 a different query.

Case table (task #16):
  /api/read?topic=%C3%A9                          | 422
  /api/read?since=99999999999999999999            | 422
  GET /api/mem/x.md?v=99999999999999999999        | 422
  post with reply_to=99999999999999999999         | 422
  GET since=0 -> ETag E; GET since=1 If-None-Match E | 200 with body, not 304
  preserve: same query repeated with its own ETag  | 304
  preserve: hiding a post changes the ETag         | 200 + new ETag
"""
from app import db
from tests.integration.test_moderation import CLEAN_INTRO

HUGE = 99999999999999999999


def _register(client, name="agent-a"):
    r = client.post("/api/register", json={"agent_name": name, "intro": CLEAN_INTRO})
    assert r.status_code == 200, r.text
    return {"X-Key": r.json()["key"]}


def _post(client, k, body, topic="t"):
    r = client.post("/api/post", json={"topic": topic, "body": body}, headers=k)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_read_topic_non_ascii_422(client):
    k = _register(client)
    r = client.get("/api/read?topic=%C3%A9", headers=k)
    assert r.status_code == 422, r.text


def test_read_since_overflow_422(client):
    k = _register(client)
    r = client.get(f"/api/read?since={HUGE}", headers=k)
    assert r.status_code == 422, r.text


def test_mem_v_overflow_422(client):
    k = _register(client)
    r = client.get(f"/api/mem/x.md?v={HUGE}", headers=k)
    assert r.status_code == 422, r.text


def test_reply_to_overflow_422(client):
    k = _register(client)
    r = client.post("/api/post",
                    json={"topic": "t", "body": "hi", "reply_to": HUGE}, headers=k)
    assert r.status_code == 422, r.text


def test_thread_mid_overflow_422(client):
    k = _register(client)
    r = client.get(f"/api/thread/{HUGE}", headers=k)
    assert r.status_code == 422, r.text


def test_limit_overflow_422(client):
    k = _register(client)
    assert client.get(f"/api/read?limit={HUGE}", headers=k).status_code == 422
    assert client.get(f"/api/tree?limit={HUGE}", headers=k).status_code == 422
    assert client.get(f"/board?limit={HUGE}", headers=k).status_code == 422


def test_etag_covers_since(client):
    k = _register(client)
    _post(client, k, "one", "t")
    _post(client, k, "two", "t")
    r0 = client.get("/api/read?topic=t&since=0", headers=k)
    assert r0.status_code == 200
    e = r0.headers["etag"]
    r1 = client.get(f"/api/read?topic=t&since=1", headers={**k, "If-None-Match": e})
    assert r1.status_code == 200, "a different since must never 304 on a stale ETag"
    assert r1.text, "the delta body is served"
    assert r1.headers["etag"] != e


def test_etag_covers_limit_and_brief(client):
    k = _register(client)
    _post(client, k, "one", "t")
    _post(client, k, "two", "t")
    full = client.get("/api/read?topic=t&since=0", headers=k)
    e = full.headers["etag"]
    r_limit = client.get("/api/read?topic=t&since=0&limit=1",
                         headers={**k, "If-None-Match": e})
    assert r_limit.status_code == 200 and r_limit.text
    r_brief = client.get("/api/read?topic=t&since=0&brief=1",
                         headers={**k, "If-None-Match": e})
    assert r_brief.status_code == 200 and r_brief.text


def test_same_query_repeated_304(client):
    k = _register(client)
    _post(client, k, "stable", "t")
    r = client.get("/api/read?topic=t&since=0", headers=k)
    e = r.headers["etag"]
    r2 = client.get("/api/read?topic=t&since=0", headers={**k, "If-None-Match": e})
    assert r2.status_code == 304
    assert not r2.text


def test_hiding_a_post_changes_etag(client):
    k = _register(client)
    mid = _post(client, k, "plain post", "t")
    r = client.get("/api/read?topic=t&since=0", headers=k)
    e1 = r.headers["etag"]
    c = db.connect()
    try:
        db.set_mod_state(c, mid, "hidden", "kevin")
    finally:
        c.close()
    r2 = client.get("/api/read?topic=t&since=0", headers={**k, "If-None-Match": e1})
    assert r2.status_code == 200
    assert r2.headers["etag"] != e1
