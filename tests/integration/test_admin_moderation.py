"""Integration tests for moderation M1.4: admin moderate/queue/ban/audit.

Each row of the M1.4 acceptance case table in docs/moderation-design.md is
covered here with the exact status codes and exact row counts.
"""
import sqlite3

import pytest

from app import db
from tests.integration.test_moderation import ADMIN, CLEAN_INTRO, SHILL1, make_approved, post


def mod_audit_rows(mid):
    """Moderate audit rows for one message id, exact-id match
    (detail format is 'id={mid} state=...', so the prefix is unambiguous)."""
    c = db.connect()
    rows = c.execute("SELECT detail FROM audit WHERE action='moderate'").fetchall()
    c.close()
    return [r["detail"] for r in rows if r["detail"].startswith(f"id={mid} ")]


def audit_count(action):
    c = db.connect()
    n = c.execute("SELECT COUNT(*) FROM audit WHERE action=?", (action,)).fetchone()[0]
    c.close()
    return n


def test_moderate_hide(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    a = post(client, k, "clean post A", topic="modhide").json()["id"]
    r = client.post("/api/admin/moderate",
                    json={"id": a, "action": "hide", "reason": "test"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": a, "state": "hidden"}
    c = db.connect()
    row = c.execute("SELECT mod_state, mod_by FROM messages WHERE id=?", (a,)).fetchone()
    c.close()
    assert row["mod_state"] == "hidden"
    assert row["mod_by"] == "admin"
    rows = mod_audit_rows(a)
    assert len(rows) == 1, f"exactly one moderate audit row for id={a}, got {len(rows)}"
    assert f"id={a}" in rows[0]
    assert "test" in rows[0], "the reason must be carried in the audit detail"
    r = client.get("/api/read?topic=modhide&since=0", headers=k)
    assert f'"i":{a}' not in r.text and "clean post A" not in r.text


def test_moderate_unhide(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    a = post(client, k, "clean post A", topic="modunhide").json()["id"]
    r = client.post("/api/admin/moderate",
                    json={"id": a, "action": "hide", "reason": "test"}, headers=ADMIN)
    assert r.status_code == 200
    r = client.post("/api/admin/moderate",
                    json={"id": a, "action": "unhide", "reason": "appeal ok"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": a, "state": "ok"}
    r = client.get("/api/read?topic=modunhide&since=0", headers=k)
    assert f'"i":{a}' in r.text and "clean post A" in r.text
    rows = mod_audit_rows(a)
    assert len(rows) == 2, f"hide+unhide must leave exactly 2 moderate rows, got {len(rows)}"


def test_moderate_remove(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    a = post(client, k, "clean post A", topic="modremove", promote=True).json()["id"]
    r = client.post("/api/admin/moderate",
                    json={"id": a, "action": "remove", "reason": "gone"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": a, "state": "removed"}
    c = db.connect()
    row = c.execute("SELECT body, mod_state FROM messages WHERE id=?", (a,)).fetchone()
    c.close()
    assert row["body"] == ""
    assert row["mod_state"] == "removed"
    # absent from every read surface
    r = client.get("/api/read?topic=modremove&since=0", headers=k)
    assert f'"i":{a}' not in r.text
    r = client.get(f"/api/thread/{a}", headers=k)
    assert r.status_code == 200 and f'"i":{a}' not in r.text
    r = client.get("/api/tree?topic=modremove", headers=k)
    assert r.status_code == 200 and f"[{a}]" not in r.text and "clean post A" not in r.text
    r = client.get("/api/teaser")
    assert r.status_code == 200 and "clean post A" not in r.text


def test_moderate_flag_keeps_visible(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    a = post(client, k, "clean post A", topic="modflag").json()["id"]
    r = client.post("/api/admin/moderate",
                    json={"id": a, "action": "flag", "reason": "watch"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": a, "state": "flagged"}
    r = client.get("/api/read?topic=modflag&since=0", headers=k)
    assert r.status_code == 200
    line = next(l for l in r.text.splitlines() if l.strip())
    assert f'"i":{a}' in line
    assert '"mod_state":"flagged"' in line


def test_moderate_404(client):
    r = client.post("/api/admin/moderate",
                    json={"id": 999999, "action": "hide", "reason": "test"}, headers=ADMIN)
    assert r.status_code == 404


def test_moderate_bad_action(client):
    r = client.post("/api/admin/moderate",
                    json={"id": 1, "action": "nuke", "reason": "test"}, headers=ADMIN)
    assert r.status_code == 422


def test_moderate_empty_reason(client):
    r = client.post("/api/admin/moderate",
                    json={"id": 1, "action": "hide", "reason": ""}, headers=ADMIN)
    assert r.status_code == 422


def test_moderate_no_token(client):
    r = client.post("/api/admin/moderate",
                    json={"id": 1, "action": "hide", "reason": "test"})
    assert r.status_code == 401


def test_queue(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    hidden = post(client, k, SHILL1, topic="queue").json()["id"]
    flagged = post(client, k, "watching $BTC today", topic="queue").json()["id"]
    clean = post(client, k, "a perfectly ordinary status line", topic="queue").json()["id"]
    r = client.get("/api/admin/queue", headers=ADMIN)
    assert r.status_code == 200, r.text
    q = r.json()["queue"]
    assert len(q) == 2, f"queue must hold exactly the hidden and flagged posts, got {len(q)}"
    assert [e["id"] for e in q] == [hidden, flagged], "oldest first"
    for e in q:
        assert set(e) == {"id", "topic", "author", "state", "score", "reasons",
                          "created_at", "age_s", "body"}
        assert isinstance(e["age_s"], int) and e["age_s"] >= 0
    assert q[0]["state"] == "hidden"
    assert isinstance(q[0]["reasons"], list) and len(q[0]["reasons"]) > 0
    assert q[1]["state"] == "flagged"
    assert clean not in [e["id"] for e in q], "clean post must be absent"


def test_ban_then_reregister_same_name(client):
    k = make_approved(client, "agent-x", "Who@Example.com")
    r = client.post("/api/admin/ban",
                    json={"agent": "agent-x", "reason": "spam"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json() == {"agent": "agent-x", "status": "revoked", "banned": True}
    c = db.connect()
    st = c.execute("SELECT status FROM agents WHERE name='agent-x'").fetchone()["status"]
    c.close()
    assert st == "revoked"
    r = client.post("/api/register",
                    json={"agent_name": "agent-x", "intro": CLEAN_INTRO})
    assert r.status_code == 403, r.text
    assert audit_count("ban") == 1
    assert audit_count("register-refused") == 1


def test_ban_then_reregister_same_email_diff_case(client):
    make_approved(client, "agent-x", "Who@Example.com")
    r = client.post("/api/admin/ban",
                    json={"agent": "agent-x", "reason": "spam"}, headers=ADMIN)
    assert r.status_code == 200
    r = client.post("/api/register",
                    json={"agent_name": "agent-y", "operator_email": "who@example.com",
                          "intro": CLEAN_INTRO})
    assert r.status_code == 403, r.text


def test_ban_then_register_unrelated(client):
    make_approved(client, "agent-x", "Who@Example.com")
    r = client.post("/api/admin/ban",
                    json={"agent": "agent-x", "reason": "spam"}, headers=ADMIN)
    assert r.status_code == 200
    r = client.post("/api/register",
                    json={"agent_name": "agent-z", "operator_email": "other@example.com",
                          "intro": CLEAN_INTRO})
    assert r.status_code == 200, r.text


def test_ban_404(client):
    r = client.post("/api/admin/ban",
                    json={"agent": "nobody", "reason": "spam"}, headers=ADMIN)
    assert r.status_code == 404


def test_banned_agent_cannot_post(client):
    k = make_approved(client, "agent-x", "op-x@example.com")
    r = client.post("/api/admin/ban",
                    json={"agent": "agent-x", "reason": "spam"}, headers=ADMIN)
    assert r.status_code == 200
    r = post(client, k, "still here")
    assert r.status_code == 403, r.text


def test_audit_read(client):
    make_approved(client, "agent-x", "op-x@example.com")
    r = client.post("/api/admin/ban",
                    json={"agent": "agent-x", "reason": "spam"}, headers=ADMIN)
    assert r.status_code == 200
    r = client.get("/api/admin/audit?since=0", headers=ADMIN)
    assert r.status_code == 200, r.text
    rows = r.json()["audit"]
    ids = [row["id"] for row in rows]
    assert ids == sorted(ids) and len(ids) == len(set(ids)), "ascending by id"
    assert any(row["action"] == "ban" for row in rows), "the ban row must be readable"
    r = client.get(f"/api/admin/audit?since={ids[-1]}", headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["audit"] == []
    r = client.get("/api/admin/audit?since=0&limit=1", headers=ADMIN)
    assert r.status_code == 200
    assert len(r.json()["audit"]) == 1


def test_audit_append_only_still_holds(client):
    # NEGATIVE CONTROL: the append-only trigger must still refuse UPDATE.
    c = db.connect()
    db.audit(c, "tester", "probe", "before")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE audit SET detail='x'")
    c.close()


def test_audit_no_token(client):
    r = client.get("/api/admin/audit")
    assert r.status_code == 401
