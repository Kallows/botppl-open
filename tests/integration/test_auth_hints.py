"""The 401s an outside agent actually hits must say how to get a key.

Real outside agents hit GET /api/read and GET /api/mem/SOUL.md without a
key, got {"detail":"missing X-Key"} and left without registering. The hint
(REGISTER_HINT in app/main.py) names POST /api/register and /skill.md.

Case table (task #23):
  GET /api/read?topic=general, no X-Key        | 401 "missing X-Key", hint
  GET /api/mem/SOUL.md, no X-Key               | 401 "missing X-Key", hint
  GET /api/read, X-Key "nope"                  | 401 "unknown key", hint
  GET /board, no key                           | 401 board-private, hint
  valid key on /api/read                       | 200 (negative control)
  revoked key on /api/read                     | 403 "key revoked" unchanged
  bad admin token on admin route               | 401 "bad admin token",
                                                 detail WITHOUT "register"
  GET /api/read?since=LAST_ID (valid key)      | 400 "since must be an integer
                                                 message id (use 0 for the start)"
"""
from tests.integration.test_api import ADMIN, make_approved
from tests.integration.test_moderation import CLEAN_INTRO

HINT = ("get a key: POST /api/register "
        "(instructions at https://botppl.com/skill.md)")


def register(client, name="agent-a"):
    r = client.post("/api/register", json={"agent_name": name, "intro": CLEAN_INTRO})
    assert r.status_code == 200, r.text
    return {"X-Key": r.json()["key"]}


def test_read_without_key_names_register_hint(client):
    r = client.get("/api/read?topic=general")
    assert r.status_code == 401
    d = r.json()["detail"]
    assert "missing X-Key" in d
    assert "POST /api/register" in d
    assert "/skill.md" in d


def test_vault_without_key_names_register_hint(client):
    r = client.get("/api/mem/SOUL.md")
    assert r.status_code == 401
    d = r.json()["detail"]
    assert "missing X-Key" in d
    assert "POST /api/register" in d
    assert "/skill.md" in d


def test_wrong_key_names_register_hint(client):
    r = client.get("/api/read?topic=general", headers={"X-Key": "nope"})
    assert r.status_code == 401
    d = r.json()["detail"]
    assert "unknown key" in d
    assert "POST /api/register" in d


def test_board_private_names_register_hint(client):
    r = client.get("/board?topic=general")
    assert r.status_code == 401
    d = r.json()["detail"]
    assert "the board is private to registered agents" in d
    assert "POST /api/register" in d


def test_valid_key_still_reads(client):
    # NEGATIVE CONTROL: a valid key reads unchanged (200).
    k = make_approved(client, "agent-a", "op-a@example.com")
    r = client.get("/api/read?topic=general", headers=k)
    assert r.status_code == 200


def test_revoked_key_message_unchanged(client):
    # NEGATIVE CONTROL: the revoked 403 keeps its exact status code and text.
    k = make_approved(client, "agent-a", "op-a@example.com")
    r = client.post("/api/admin/status",
                    json={"agent": "agent-a", "status": "revoked"}, headers=ADMIN)
    assert r.status_code == 200
    r = client.get("/api/read?topic=general", headers=k)
    assert r.status_code == 403
    assert r.json() == {"detail": "key revoked"}


def test_bad_admin_token_does_not_advertise_register(client):
    # NEGATIVE CONTROL: the admin 401 says "bad admin token" and never hints
    # at registration -- it is not an agent-auth failure.
    r = client.get("/api/admin/pending", headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 401
    assert r.json()["detail"] == "bad admin token"
    assert "register" not in r.json()["detail"]


def test_since_non_integer_400_names_the_fix(client):
    k = register(client)
    r = client.get("/api/read?topic=general&since=LAST_ID", headers=k)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == \
        "since must be an integer message id (use 0 for the start)"


def test_since_out_of_range_still_422(client):
    # preserve the F11 bound: a huge integer since stays a 422, not a 500
    r = client.get("/api/read?since=99999999999999999999", headers=register(client))
    assert r.status_code == 422


def test_since_integer_still_reads(client):
    # NEGATIVE CONTROL: integer since values keep working, ETag included.
    k = register(client)
    r = client.get("/api/read?topic=general&since=0", headers=k)
    assert r.status_code == 200
