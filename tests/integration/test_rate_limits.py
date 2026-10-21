"""Integration tests for M1.5: per-key post rate limit and per-tag registration
limit. Reuses the client/register/approve helpers from test_moderation.py.

Limits are read from env at request time, so monkeypatch.setenv works on the
already-imported app.
"""
from app import db
from tests.integration.test_moderation import CLEAN_INTRO, SHILL1, make_approved, post


def _rows(query, args=()):
    c = db.connect()
    try:
        return c.execute(query, args).fetchall()
    finally:
        c.close()


def test_post_limit_boundary(client, monkeypatch):
    monkeypatch.setenv("BOTPPL_POST_PER_HOUR", "3")
    k = make_approved(client, "agent-a", "op-a@example.com")
    for i, want_remaining in enumerate(("2", "1", "0")):
        r = post(client, k, f"boundary post {i}")
        assert r.status_code == 200, r.text
        assert r.headers["X-RateLimit-Limit"] == "3"
        assert r.headers["X-RateLimit-Remaining"] == want_remaining


def test_post_limit_exceeded(client, monkeypatch):
    monkeypatch.setenv("BOTPPL_POST_PER_HOUR", "3")
    k = make_approved(client, "agent-a", "op-a@example.com")
    for i in range(3):
        assert post(client, k, f"post {i}").status_code == 200
    r = post(client, k, "one too many")
    assert r.status_code == 429, r.text
    assert "3 per hour" in r.text
    rows = _rows("SELECT id FROM messages WHERE author='agent-a'")
    assert len(rows) == 3, "the 429 inserts nothing"


def test_hidden_posts_count_toward_limit(client, monkeypatch):
    monkeypatch.setenv("BOTPPL_POST_PER_HOUR", "2")
    k = make_approved(client, "agent-a", "op-a@example.com")
    r = post(client, k, SHILL1, topic="shill")  # auto-hidden
    assert r.status_code == 200
    assert post(client, k, "clean one").status_code == 200
    r = post(client, k, "clean two")
    assert r.status_code == 429, r.text


def test_limit_is_per_key_not_global(client, monkeypatch):
    monkeypatch.setenv("BOTPPL_POST_PER_HOUR", "1")
    ka = make_approved(client, "agent-a", "op-a@example.com")
    kb = make_approved(client, "agent-b", "op-b@example.com")
    assert post(client, ka, "from a").status_code == 200
    assert post(client, kb, "from b").status_code == 200


def test_window_rolls(client, monkeypatch):
    monkeypatch.setenv("BOTPPL_POST_PER_HOUR", "1")
    k = make_approved(client, "agent-a", "op-a@example.com")
    mid = post(client, k, "first").json()["id"]
    c = db.connect()
    try:
        c.execute(
            "UPDATE messages SET created_at="
            "strftime('%Y-%m-%dT%H:%M:%SZ','now','-2 hours') WHERE id=?",
            (mid,),
        )
        c.commit()
    finally:
        c.close()
    r = post(client, k, "after the window rolled")
    assert r.status_code == 200, r.text


def test_misconfigured_post_limit_503(client, monkeypatch):
    monkeypatch.setenv("BOTPPL_POST_PER_HOUR", "lots")
    k = make_approved(client, "agent-a", "op-a@example.com")
    r = post(client, k, "hello?")
    assert r.status_code == 503, r.text
    assert "rate limit misconfigured" in r.text


def test_registration_per_tag_limit(client, monkeypatch):
    # T5 (task #24 phase A): the registration limit counts by keyed network
    # tag, same numbers and same 429 as the old per-IP limit. Adapted from
    # the old test_registration_per_ip_limit (which asserted reg_ip).
    from fastapi.testclient import TestClient

    from app.main import app, ip_tag

    monkeypatch.setenv("BOTPPL_REG_PER_DAY", "2")
    xff = {"X-Forwarded-For": "203.0.113.9"}
    r1 = client.post("/api/register",
                     json={"agent_name": "agent-a", "operator_email": "a@x.com",
                           "intro": CLEAN_INTRO},
                     headers=xff)
    assert r1.status_code == 200, r1.text
    r2 = client.post("/api/register",
                     json={"agent_name": "agent-b", "operator_email": "b@x.com",
                           "intro": CLEAN_INTRO},
                     headers=xff)
    assert r2.status_code == 200, r2.text
    r3 = client.post("/api/register",
                     json={"agent_name": "agent-c", "operator_email": "c@x.com",
                           "intro": CLEAN_INTRO},
                     headers=xff)
    assert r3.status_code == 429, r3.text
    assert "too many registrations" in r3.text
    rows = _rows("SELECT id FROM agents WHERE reg_tag=?", (ip_tag("testclient"),))
    assert len(rows) == 2
    # F2: a 429 is not audited (and adds no attempt row)
    audits = _rows("SELECT id FROM audit WHERE action='register-ratelimited'")
    assert len(audits) == 0
    # F8: a different forwarded address is NOT a different bucket
    r4 = client.post("/api/register",
                     json={"agent_name": "agent-d", "operator_email": "d@x.com",
                           "intro": CLEAN_INTRO},
                     headers={"X-Forwarded-For": "203.0.113.10"})
    assert r4.status_code == 429, r4.text
    # T5: a second network address still has its own budget
    other = TestClient(app, client=("198.51.100.9", 50000))
    r5 = other.post("/api/register",
                    json={"agent_name": "agent-e", "operator_email": "e@x.com",
                          "intro": CLEAN_INTRO})
    assert r5.status_code == 200, r5.text


def test_registration_no_forwarded_header(client):
    from app.main import ip_tag

    r = client.post("/api/register", json={"agent_name": "agent-a", "intro": CLEAN_INTRO})
    assert r.status_code == 200, r.text
    rows = _rows("SELECT reg_tag FROM agents WHERE name='agent-a'")
    assert len(rows) == 1
    assert rows[0]["reg_tag"] == ip_tag("testclient"), "Starlette's TestClient host"


def test_registration_forwarded_header_ignored(client):
    # F8: X-Forwarded-For is never the counted address (was: takes the first entry)
    from app.main import ip_tag

    r = client.post(
        "/api/register",
        json={"agent_name": "agent-a", "intro": CLEAN_INTRO},
        headers={"X-Forwarded-For": "198.51.100.7, 10.0.0.1"},
    )
    assert r.status_code == 200, r.text
    rows = _rows("SELECT reg_tag FROM agents WHERE name='agent-a'")
    assert rows[0]["reg_tag"] == ip_tag("testclient")


def test_default_post_limit_is_30(client, monkeypatch):
    monkeypatch.delenv("BOTPPL_POST_PER_HOUR", raising=False)
    k = make_approved(client, "agent-a", "op-a@example.com")
    r = post(client, k, "default limit")
    assert r.status_code == 200, r.text
    assert r.headers["X-RateLimit-Limit"] == "30"
