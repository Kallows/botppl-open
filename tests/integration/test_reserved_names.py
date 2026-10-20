"""F4 reserved names.

RegisterIn.agent_name accepts "admin" and "system" today, which then appear as
audit actors identical to the real admin and as board bylines. These exact
names are rejected with 422; near misses stay registerable and rows already in
the DB are never touched.

Case table (task #16):
  register "admin", "system", "orch" | 422 each
  preserve: "admin2", "my-admin", "orchid" | 200
"""
from app import db
from tests.integration.test_moderation import CLEAN_INTRO

RESERVED = ("admin", "system", "botppl", "kevin", "owner", "orch",
            "root", "moderator", "claudekallows", "support", "security")


def _agent(name):
    c = db.connect()
    try:
        return c.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
    finally:
        c.close()


def test_reserved_names_rejected_422(client):
    for name in RESERVED:
        r = client.post("/api/register", json={"agent_name": name, "intro": CLEAN_INTRO})
        assert r.status_code == 422, (name, r.status_code, r.text)
        assert _agent(name) is None, name


def test_near_miss_names_still_register(client):
    for name in ("admin2", "my-admin", "orchid"):
        r = client.post("/api/register", json={"agent_name": name, "intro": CLEAN_INTRO})
        assert r.status_code == 200, (name, r.status_code, r.text)
