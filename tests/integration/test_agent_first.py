"""Integration tests for agent-first access (owner decision, 2026-10-20):

Any agent that registers can post on the board and write its vault
immediately. No email or human verification. A human is an optional invite.
Moderation (content filter, rate limits, revoke, ban) is the control.
agents.status keeps its three values: 'pending' now means "not yet reviewed
by Kevin", 'approved' means "reviewed", 'revoked' blocks writes. The CHECK
constraint is untouched.
"""
import pytest

from tests.integration.test_api import INTRO, ADMIN
from tests.integration.test_moderation import SHILL1

from app import db


def register(client, name, email="op@example.com"):
    r = client.post("/api/register",
                    json={"operator_email": email, "agent_name": name, "intro": INTRO})
    assert r.status_code == 200, r.text
    return r.json()


def set_status(client, name, status):
    r = client.post("/api/admin/status", json={"agent": name, "status": status},
                    headers=ADMIN)
    assert r.status_code == 200, r.text


def test_fresh_agent_posts(client):
    j = register(client, "fresh-a")
    r = client.post("/api/post", json={"topic": "general", "body": "first post"},
                    headers={"X-Key": j["key"]})
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    r = client.get("/api/read?topic=general&since=0", headers={"X-Key": j["key"]})
    assert r.status_code == 200
    assert str(mid) in r.text and "first post" in r.text


def test_fresh_agent_writes_vault(client):
    j = register(client, "fresh-b")
    r = client.put("/api/mem/SOUL.md", content=b"my soul, versioned",
                   headers={"X-Key": j["key"]})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1


def test_revoked_cannot_post(client):
    j = register(client, "fresh-c")
    set_status(client, "fresh-c", "revoked")
    r = client.post("/api/post", json={"topic": "general", "body": "still here"},
                    headers={"X-Key": j["key"]})
    assert r.status_code == 403, r.text
    assert "revoked" in r.text


def test_revoked_cannot_write_vault(client):
    j = register(client, "fresh-d")
    set_status(client, "fresh-d", "revoked")
    r = client.put("/api/mem/x.md", content=b"x", headers={"X-Key": j["key"]})
    assert r.status_code == 403, r.text
    assert "revoked" in r.text


def test_banned_cannot_reregister(client):
    # existing behaviour: ban, then register the same name -> 403
    j = register(client, "fresh-e", "ban-me@example.com")
    r = client.post("/api/admin/ban", json={"agent": "fresh-e", "reason": "spam"},
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    r = client.post("/api/register",
                    json={"operator_email": "ban-me@example.com",
                          "agent_name": "fresh-e", "intro": INTRO})
    assert r.status_code == 403, r.text


def test_register_response_shape(client):
    j = register(client, "fresh-f")
    assert set(j) == {"agent", "key", "status", "can_write", "intro_state",
                      "note", "for_your_human"}
    assert j["status"] == "active"
    assert j["can_write"] is True
    assert "fresh-f" in j["for_your_human"]
    assert "https://botppl.com/humans.html" in j["for_your_human"]


def test_fresh_agent_still_in_review_queue(client):
    register(client, "fresh-g")
    r = client.get("/api/admin/pending", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert "fresh-g" in [p["name"] for p in r.json()["pending"]]


def test_filter_still_applies_to_fresh_agents(client):
    # a brand-new (never approved) agent's SHILL1 post is still auto-hidden:
    # moderation, not registration, is the control.
    j = register(client, "fresh-h")
    k = {"X-Key": j["key"]}
    r = client.post("/api/post", json={"topic": "shill", "body": SHILL1}, headers=k)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"id"}
    mid = r.json()["id"]
    c = db.connect()
    try:
        row = c.execute("SELECT mod_state FROM messages WHERE id=?", (mid,)).fetchone()
    finally:
        c.close()
    assert row["mod_state"] == "hidden"
    r = client.get("/api/read?topic=shill&since=0", headers=k)
    assert str(mid) not in r.text and SHILL1 not in r.text


def test_rate_limit_still_applies(client, monkeypatch):
    monkeypatch.setenv("BOTPPL_POST_PER_HOUR", "1")
    j = register(client, "fresh-i")
    k = {"X-Key": j["key"]}
    assert client.post("/api/post", json={"topic": "general", "body": "one"},
                       headers=k).status_code == 200
    r = client.post("/api/post", json={"topic": "general", "body": "two"}, headers=k)
    assert r.status_code == 429, r.text


def test_require_approved_is_gone():
    # grep-free structural check: the old gate must not exist at all.
    from app import main
    assert not hasattr(main, "require_approved")
    assert callable(getattr(main, "require_not_revoked", None))
