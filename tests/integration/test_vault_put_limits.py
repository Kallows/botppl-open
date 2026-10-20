"""F3 vault PUT limits.

- Per-key PUT limit BOTPPL_PUT_PER_HOUR (default 60) counted from mem_versions
  rows by the agent in the last hour; 429 {"detail":"too many vault writes this
  hour"} and not audited. Same _env_int pattern as posts: non-integer -> 503.
- Quota accounting: every version counts as max(len(body), 1024) bytes against
  MAX_MEM_TOTAL, so empty PUTs are no longer free and at most 10240 versions
  fit per agent.

Case table (task #16):
  BOTPPL_PUT_PER_HOUR=3: four PUTs                | 200, 200, 200, 429
  empty-body PUTs: quota accounting               | N empty PUTs use N*1024
                                                    bytes; MAX_MEM_TOTAL=3*1024
                                                    -> 4th empty PUT 413
  preserve: 1-byte PUT                            | 200, X-SHA256 == sha256(byte)
  preserve: 10 MB quota message/status (real data)| unchanged
"""
import hashlib

from app import db
from app.main import MAX_MEM_FILE, MAX_MEM_TOTAL
from tests.integration.test_moderation import CLEAN_INTRO


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


def test_put_per_hour_limit(client, monkeypatch):
    monkeypatch.setenv("BOTPPL_PUT_PER_HOUR", "3")
    k = _register(client)
    for i in range(3):
        r = client.put(f"/api/mem/f{i}.md", content=b"x", headers=k)
        assert r.status_code == 200, r.text
    audit_before = len(_rows("SELECT * FROM audit WHERE action='mem-put'"))
    r = client.put("/api/mem/f3.md", content=b"x", headers=k)
    assert r.status_code == 429, r.text
    assert r.json() == {"detail": "too many vault writes this hour"}
    assert len(_rows("SELECT * FROM audit WHERE action='mem-put'")) == audit_before, \
        "a 429 is not audited"


def test_put_limit_misconfigured_503(client, monkeypatch):
    monkeypatch.setenv("BOTPPL_PUT_PER_HOUR", "lots")
    k = _register(client)
    r = client.put("/api/mem/x.md", content=b"x", headers=k)
    assert r.status_code == 503, r.text
    assert "rate limit misconfigured" in r.text


def test_empty_put_counts_exactly_1024_bytes(client, monkeypatch):
    k = _register(client)
    monkeypatch.setattr("app.main.MAX_MEM_TOTAL", 1024)
    r1 = client.put("/api/mem/a.md", content=b"", headers=k)
    assert r1.status_code == 200, r1.text
    r2 = client.put("/api/mem/a.md", content=b"", headers=k)
    assert r2.status_code == 413, \
        "quota after N empty PUTs is N*1024: one empty version fills 1024 bytes"


def test_empty_put_quota_is_n_times_1024(client, monkeypatch):
    k = _register(client)
    monkeypatch.setattr("app.main.MAX_MEM_TOTAL", 3 * 1024)
    for i in range(3):
        r = client.put(f"/api/mem/f{i}.md", content=b"", headers=k)
        assert r.status_code == 200, (i, r.text)
    r = client.put("/api/mem/f3.md", content=b"", headers=k)
    assert r.status_code == 413, "the 4th empty PUT exceeds 3*1024 counted bytes"


def test_one_byte_put_sha256_preserved(client):
    k = _register(client)
    r = client.put("/api/mem/x.md", content=b"x", headers=k)
    assert r.status_code == 200, r.text
    g = client.get("/api/mem/x.md", headers=k)
    assert g.status_code == 200
    assert g.headers["X-SHA256"] == hashlib.sha256(b"x").hexdigest()
    assert g.content == b"x"


def test_ten_mb_quota_message_status_unchanged(client):
    k = _register(client)
    chunk = b"x" * MAX_MEM_FILE
    for i in range(40):
        r = client.put(f"/api/mem/f{i}.md", content=chunk, headers=k)
        assert r.status_code == 200, (i, r.text)
    assert _rows("SELECT COALESCE(SUM(MAX(LENGTH(body),1024)),0) s "
                 "FROM mem_versions")[0]["s"] == 40 * MAX_MEM_FILE == MAX_MEM_TOTAL
    r = client.put("/api/mem/f40.md", content=chunk, headers=k)
    assert r.status_code == 413
    assert r.json() == {"detail": "memory quota exceeded; contact the owner"}
