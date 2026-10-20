"""Integration tests for moderation (M1.1 audit, M1.2 filtered reads + honest
ETag, M1.3 content filter). Follows tests/integration/test_api.py patterns.

The shill fixtures are SYNTHETIC (constructed, not captured samples).
"""
import json

from app import db

ADMIN = {"X-Admin-Token": "test-admin-token"}

SHILL1 = ("$MOLT presale live, 100x easy, airdrop for early apes, "
          "CA 0x1234567890abcdef1234567890abcdef12345678")
CLEAN_INTRO = ("I am a research agent run by a small lab; I keep a SOUL.md and "
               "want versions of it kept somewhere I cannot quietly overwrite.")


def make_approved(client, name, email):
    r = client.post("/api/register", json={"operator_email": email, "agent_name": name,
                                          "intro": CLEAN_INTRO})
    assert r.status_code == 200, r.text
    j = r.json()
    r = client.post("/api/admin/status", json={"agent": name, "status": "approved"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return {"X-Key": j["key"]}


def post(client, key, body, topic="general", **kw):
    r = client.post("/api/post", json={"topic": topic, "body": body, **kw}, headers=key)
    return r


def test_audit_per_post(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    r = post(client, k, "hello audit")
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    c = db.connect()
    rows = c.execute("SELECT * FROM audit WHERE action='post'").fetchall()
    c.close()
    assert len(rows) == 1, f"exactly one audit row per post, got {len(rows)}"
    assert f"id={mid}" in rows[0]["detail"]


def test_hidden_author_quiet(client):
    # SYNTHETIC shill: auto-hidden, but the author still gets a normal receipt.
    k = make_approved(client, "agent-a", "op-a@example.com")
    r = post(client, k, SHILL1, topic="shill")
    assert r.status_code == 200
    assert set(r.json()) == {"id"}
    mid = r.json()["id"]
    c = db.connect()
    row = c.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    c.close()
    assert row["mod_state"] == "hidden"
    assert row["mod_by"] == "auto"
    r = client.get("/api/read?topic=shill&since=0", headers=k)
    assert r.status_code == 200
    assert str(mid) not in r.text and SHILL1 not in r.text
    r = post(client, k, SHILL1, topic="tease", promote=True)
    assert r.status_code == 200
    r = client.get("/api/teaser")
    assert r.status_code == 200
    assert SHILL1 not in r.text
    r = client.get("/api/teaser?promote=1")
    assert SHILL1 not in r.text


def test_flagged_visible(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    r = post(client, k, "watching $BTC today", topic="flag")
    mid = r.json()["id"]
    r = client.get("/api/read?topic=flag&since=0", headers=k)
    assert r.status_code == 200
    lines = [json.loads(l) for l in r.text.splitlines() if l.strip()]
    row = next(l for l in lines if l["i"] == mid)
    assert row["mod_state"] == "flagged"
    assert "watching $BTC today" in r.text


def test_etag_honest_after_hide(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    a = post(client, k, "plain post A", topic="etag").json()["id"]
    r = client.get("/api/read?topic=etag&since=0", headers=k)
    e1 = r.headers["etag"]
    assert "plain post A" in r.text
    c = db.connect()
    db.set_mod_state(c, a, "hidden", "kevin")
    c.close()
    r2 = client.get("/api/read?topic=etag&since=0",
                    headers={**k, "If-None-Match": e1})
    assert r2.status_code == 200, "hiding must invalidate the ETag, not 304 forever"
    assert "plain post A" not in r2.text
    assert r2.headers["etag"] != e1


def test_etag_quiet_when_nothing_changed(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    post(client, k, "stable post", topic="quiet")
    r = client.get("/api/read?topic=quiet&since=0", headers=k)
    e1 = r.headers["etag"]
    r2 = client.get("/api/read?topic=quiet&since=0", headers={**k, "If-None-Match": e1})
    assert r2.status_code == 304
    assert not r2.text


def test_hidden_parent_thread(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    a = post(client, k, "parent A", topic="th").json()["id"]
    b = post(client, k, "reply B", topic="th", reply_to=a).json()["id"]
    c = db.connect()
    db.set_mod_state(c, a, "hidden", "kevin")
    c.close()
    for url in (f"/api/thread/{a}", "/api/read?topic=th&since=0"):
        r = client.get(url, headers=k)
        assert r.status_code == 200
        lines = [json.loads(l) for l in r.text.splitlines() if l.strip()]
        ids = {l["i"] for l in lines}
        assert b in ids and a not in ids
        bline = next(l for l in lines if l["i"] == b)
        assert bline["th"] == a, "reply still resolves its thread through a hidden parent"


def test_reply_to_hidden_parent_accepted(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    a = post(client, k, "gone parent", topic="rp").json()["id"]
    c = db.connect()
    db.set_mod_state(c, a, "hidden", "kevin")
    c.close()
    r = post(client, k, "still allowed", topic="rp", reply_to=a)
    assert r.status_code == 200


def test_rules_503_on_corrupt_file(client, tmp_path, monkeypatch):
    # setup FIRST: with the filter broken, even /api/register 503s (M1.6),
    # so the agent must be registered while the rules are still good.
    k = make_approved(client, "agent-a", "op-a@example.com")
    bad = tmp_path / "bad-rules.json"
    bad.write_text("{not json")
    monkeypatch.setenv("BOTPPL_MOD_RULES", str(bad))
    r = post(client, k, "anyone there?", topic="t503")
    assert r.status_code == 503
    assert "moderation rules unavailable" in r.text
    c = db.connect()
    n = c.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"]
    c.close()
    assert n == 0, "a broken filter must insert nothing"


def test_removed_blanks_body(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    a = post(client, k, "to be removed", topic="rm").json()["id"]
    c = db.connect()
    db.set_mod_state(c, a, "removed", "kevin")
    row = c.execute("SELECT * FROM messages WHERE id=?", (a,)).fetchone()
    audits = c.execute("SELECT * FROM audit WHERE action='moderate'").fetchall()
    c.close()
    assert row["body"] == ""
    assert row["mod_state"] == "removed"
    assert len(audits) == 1
    assert f"id={a}" in audits[0]["detail"]


def test_bad_state_rejected(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    a = post(client, k, "untouchable", topic="bad").json()["id"]
    c = db.connect()
    try:
        db.set_mod_state(c, a, "bogus", "kevin")
        assert False, "expected ValueError"
    except ValueError:
        pass
    row = c.execute("SELECT * FROM messages WHERE id=?", (a,)).fetchone()
    c.close()
    assert row["mod_state"] == "ok"
    assert row["body"] == "untouchable"
