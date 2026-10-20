"""F1: app-level request-body cap (MAX_BODY = 512 KB), enforced as ASGI
middleware outside routing so it runs before FastAPI parses pydantic bodies.
Caddy already caps at 512 KB live; this protects any other deployment.

Case table (task #16):
  POST /api/register, 600 KB body (Content-Length)  | 413 {"detail":"request body too large"}
  POST /api/register, 600 KB chunked (no C-L)       | 413 (stops once over the cap)
  preserve: 256 KB vault PUT (MAX_MEM_FILE)         | 200
  preserve: 16 KB board post                        | 200
  preserve: 300 KB vault PUT                        | 413 "file over 262144 bytes" (unchanged)
"""
from app import db
from app.main import MAX_MEM_FILE, MAX_POST
from tests.integration.test_moderation import CLEAN_INTRO

BIG = 600 * 1024


def _rows(q, a=()):
    c = db.connect()
    try:
        return c.execute(q, a).fetchall()
    finally:
        c.close()


def _register(client, name="agent-a"):
    r = client.post("/api/register", json={"agent_name": name, "intro": CLEAN_INTRO})
    assert r.status_code == 200, r.text
    return {"X-Key": r.json()["key"]}


def test_register_600kb_body_413(client):
    r = client.post("/api/register", content=b"x" * BIG,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413, r.text
    assert r.json() == {"detail": "request body too large"}
    assert _rows("SELECT id FROM agents") == [], "rejected before anything parsed"


def test_register_600kb_chunked_413(client):
    # no Content-Length header at all: the middleware must stream-count and
    # answer 413 as soon as the running total exceeds MAX_BODY
    chunks = (b"x" * 65536 for _ in range(BIG // 65536))
    r = client.post("/api/register", content=chunks,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413, r.text
    assert r.json() == {"detail": "request body too large"}
    assert _rows("SELECT id FROM agents") == [], "rejected before anything parsed"


def test_vault_put_at_mem_file_cap_200(client):
    k = _register(client)
    r = client.put("/api/mem/SOUL.md", content=b"x" * MAX_MEM_FILE, headers=k)
    assert r.status_code == 200, r.text


def test_board_post_16kb_200(client):
    k = _register(client)
    r = client.post("/api/post", json={"topic": "general", "body": "x" * MAX_POST},
                    headers=k)
    assert r.status_code == 200, r.text


def test_vault_put_300kb_413_mem_file_message_unchanged(client):
    k = _register(client)
    r = client.put("/api/mem/SOUL.md", content=b"x" * (300 * 1024), headers=k)
    assert r.status_code == 413, r.text
    assert r.json() == {"detail": f"file over {MAX_MEM_FILE} bytes"}
