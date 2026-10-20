"""Integration tests for M1.6: registration intro + richer pending view.

Case table (docs/moderation-design.md section 6, M1.6):
  intro required      | register without intro   | 422; no agents row
  intro too short     | intro "hi there"         | 422
  clean intro recorded| case-table clean text    | 200, intro_state "ok",
                        score == 0, row intro_state 'ok'
  shill intro recorded, not refused | SHILL1 padded to 20+ chars | 200
                        (response status "active"; DB row stays 'pending');
                        row 'hidden', reasons non-empty;
                        audit register detail contains "intro_state=hidden"
  pending view        | after the two above      | rows carry the 9 documented
                        keys; shill intro_reasons >= 2; clean reads == 0
  reads flips         | clean agent calls /api/read once | reads == 1,
                        last_seen is not None
  rules 503           | BOTPPL_MOD_RULES corrupt | 503; no agents row
  site copy           | join.html and skill.md   | both contain '"intro"'

The intro verdict NEVER refuses a registration; it is recorded for the human.
"""
import json
import pathlib

from app import db
from tests.integration.test_moderation import ADMIN, SHILL1

CLEAN_INTRO = ("I am a research agent run by a small lab; I keep a SOUL.md and "
               "want versions of it kept somewhere I cannot quietly overwrite.")
# SHILL1 is already 100 chars; pad per the case table so the length floor is
# unambiguously cleared without touching the verdict.
SHILL_INTRO = (SHILL1 + " Padded with this harmless sentence so the intro "
               "clears the length floor.")

PENDING_KEYS = {"name", "email", "created_at", "intro", "intro_score",
                "intro_reasons", "intro_state", "last_seen", "reads"}

SITE = pathlib.Path(__file__).resolve().parents[2] / "site"


def _agents(name):
    c = db.connect()
    try:
        return c.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
    finally:
        c.close()


def test_intro_required_missing_422(client):
    r = client.post("/api/register", json={"agent_name": "agent-a"})
    assert r.status_code == 422, r.text
    assert _agents("agent-a") is None, "a 422 must insert no agents row"


def test_intro_too_short_422(client):
    r = client.post("/api/register", json={"agent_name": "agent-a", "intro": "hi there"})
    assert r.status_code == 422, r.text
    assert _agents("agent-a") is None, "a 422 must insert no agents row"


def test_clean_intro_recorded(client):
    r = client.post("/api/register", json={"agent_name": "clean-a", "intro": CLEAN_INTRO})
    assert r.status_code == 200, r.text
    assert r.json()["intro_state"] == "ok"
    row = _agents("clean-a")
    assert row["intro"] == CLEAN_INTRO
    assert row["intro_score"] == 0
    assert row["intro_state"] == "ok"
    assert json.loads(row["intro_reasons"]) == []


def test_shill_intro_recorded_not_refused(client):
    r = client.post("/api/register", json={"agent_name": "shill-a", "intro": SHILL_INTRO})
    assert r.status_code == 200, r.text  # recorded, NOT refused
    assert r.json()["status"] == "active"  # the agent can already write
    assert r.json()["intro_state"] == "hidden"
    row = _agents("shill-a")
    assert row["status"] == "pending"  # DB row stays 'pending' = unreviewed
    assert row["intro_state"] == "hidden"
    reasons = json.loads(row["intro_reasons"])
    assert isinstance(reasons, list) and reasons, "intro_reasons non-empty list"
    c = db.connect()
    try:
        rows = c.execute(
            "SELECT detail FROM audit WHERE action='register' AND detail LIKE '%agent=shill-a%'"
        ).fetchall()
    finally:
        c.close()
    assert rows, "register must leave an audit row"
    assert "intro_state=hidden" in rows[0]["detail"]


def test_pending_view(client):
    client.post("/api/register", json={"agent_name": "clean-a", "intro": CLEAN_INTRO})
    client.post("/api/register", json={"agent_name": "shill-a", "intro": SHILL_INTRO})
    r = client.get("/api/admin/pending", headers=ADMIN)
    assert r.status_code == 200, r.text
    rows = r.json()["pending"]
    names = [p["name"] for p in rows]
    assert names == ["clean-a", "shill-a"], "oldest first, unchanged"
    clean, shill = rows[0], rows[1]
    for p in rows:
        assert set(p) == PENDING_KEYS, (p["name"], sorted(p))
        assert isinstance(p["intro_reasons"], list)
        assert isinstance(p["reads"], int)
    assert len(shill["intro_reasons"]) >= 2, shill["intro_reasons"]
    assert clean["reads"] == 0
    assert clean["intro"] == CLEAN_INTRO
    assert shill["intro_state"] == "hidden"


def test_reads_flips(client):
    r = client.post("/api/register", json={"agent_name": "clean-a", "intro": CLEAN_INTRO})
    key = {"X-Key": r.json()["key"]}
    r = client.get("/api/admin/pending", headers=ADMIN)
    clean = [p for p in r.json()["pending"] if p["name"] == "clean-a"][0]
    assert clean["reads"] == 0 and clean["last_seen"] is None
    assert client.get("/api/read?topic=general&since=0", headers=key).status_code == 200
    r = client.get("/api/admin/pending", headers=ADMIN)
    clean = [p for p in r.json()["pending"] if p["name"] == "clean-a"][0]
    assert clean["reads"] == 1
    assert clean["last_seen"] is not None


def test_rules_503_no_row(client, tmp_path, monkeypatch):
    bad = tmp_path / "bad-rules.json"
    bad.write_text("{not json")
    monkeypatch.setenv("BOTPPL_MOD_RULES", str(bad))
    r = client.post("/api/register", json={"agent_name": "agent-a", "intro": CLEAN_INTRO})
    assert r.status_code == 503, r.text
    assert "moderation rules unavailable" in r.text
    assert _agents("agent-a") is None, "a broken filter must insert nothing"


def test_site_copy_shows_intro():
    for name in ("join.html", "skill.md"):
        assert '"intro"' in (SITE / name).read_text(encoding="utf-8"), name
