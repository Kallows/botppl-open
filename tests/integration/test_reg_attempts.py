"""F2 registration attempt accounting + F8 counting IP.

F2 new order: request validation -> per-IP attempt limit -> ban -> name taken
-> insert. Every attempt that reaches the limit check is counted in the new
reg_attempts table -- successes, ban refusals and name-taken refusals -- so a
banned email or a taken name cannot be probed without limit (ban-list oracle).
A 429 is neither audited nor counted. Rows older than 2 days are pruned on
insert. F8: the counted IP is request.client.host, never X-Forwarded-For
(uvicorn's proxy-headers handling already trusts that only from Caddy on
127.0.0.1), so a rotating header cannot open fresh buckets.

Case table (task #16):
  BOTPPL_REG_PER_DAY=2: register a, b, c from one IP      | 200, 200, 429
  BOTPPL_REG_PER_DAY=2: banned email twice, then fresh    | 403, 403, 429
  BOTPPL_REG_PER_DAY=2: name-taken twice, then fresh      | 409, 409, 429
  10 requests after the limit is reached                  | all 429, audit +0 rows
  preserve: banned email under the limit                  | 403
  preserve: 5 fresh names (default limit)                 | 200 x5, 6th 429
  F8: rotating X-Forwarded-For, BOTPPL_REG_PER_DAY=2      | third registration 429
"""
from app import db
from tests.integration.test_moderation import ADMIN, CLEAN_INTRO


def _rows(q, a=()):
    c = db.connect()
    try:
        return c.execute(q, a).fetchall()
    finally:
        c.close()


def _register(client, name, email=None, headers=None):
    payload = {"agent_name": name, "intro": CLEAN_INTRO}
    if email:
        payload["operator_email"] = email
    return client.post("/api/register", json=payload, headers=headers)


def test_successes_count_toward_limit(client, monkeypatch):
    monkeypatch.setenv("BOTPPL_REG_PER_DAY", "2")
    assert _register(client, "agent-a").status_code == 200
    assert _register(client, "agent-b").status_code == 200
    assert _register(client, "agent-c").status_code == 429
    rows = _rows("SELECT * FROM reg_attempts")
    assert len(rows) == 2, "one attempt row per attempt that reached the limit check"


def test_banned_email_refusals_count_toward_limit(client, monkeypatch):
    monkeypatch.setenv("BOTPPL_REG_PER_DAY", "2")
    c = db.connect()
    c.execute("INSERT INTO banned(name,email,reason,banned_by) "
              "VALUES(NULL,'banned@x.com','spam','admin')")
    c.commit()
    c.close()
    assert _register(client, "try-1", "banned@x.com").status_code == 403
    assert _register(client, "try-2", "banned@x.com").status_code == 403
    r = _register(client, "fresh", "ok@x.com")
    assert r.status_code == 429, "two refusals already spent the day's attempts"


def test_name_taken_refusals_count_toward_limit(client, monkeypatch):
    monkeypatch.setenv("BOTPPL_REG_PER_DAY", "2")
    c = db.connect()
    c.execute("INSERT INTO agents(name,key_hash) VALUES('taken','preexisting-hash')")
    c.commit()
    c.close()
    assert _register(client, "taken").status_code == 409
    assert _register(client, "taken").status_code == 409
    r = _register(client, "fresh", "ok@x.com")
    assert r.status_code == 429, "two refusals already spent the day's attempts"


def test_after_limit_no_audit_no_attempt_row(client, monkeypatch):
    monkeypatch.setenv("BOTPPL_REG_PER_DAY", "2")
    assert _register(client, "agent-a").status_code == 200
    assert _register(client, "agent-b").status_code == 200
    audit_before = len(_rows("SELECT * FROM audit"))
    att_before = len(_rows("SELECT * FROM reg_attempts"))
    for i in range(10):
        assert _register(client, f"spam-{i}").status_code == 429
    assert len(_rows("SELECT * FROM audit")) == audit_before, \
        "a 429 writes no audit row"
    assert len(_rows("SELECT * FROM reg_attempts")) == att_before, \
        "a 429 adds no attempt row"


def test_banned_email_under_limit_still_403(client):
    assert _register(client, "agent-x", "who@x.com").status_code == 200
    r = client.post("/api/admin/ban",
                    json={"agent": "agent-x", "reason": "spam"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert _register(client, "agent-y", "who@x.com").status_code == 403


def test_default_limit_five_fresh_names(client):
    for i in range(5):
        assert _register(client, f"agent-{i}").status_code == 200
    assert _register(client, "agent-6").status_code == 429


def test_attempt_rows_pruned_after_two_days(client):
    c = db.connect()
    c.execute("INSERT INTO reg_attempts(ip,at) VALUES('testclient',"
              "strftime('%Y-%m-%dT%H:%M:%SZ','now','-3 days'))")
    c.commit()
    c.close()
    assert _register(client, "agent-a").status_code == 200
    rows = _rows("SELECT * FROM reg_attempts")
    assert len(rows) == 1, "the 3-day-old attempt row was pruned on insert"


def test_forwarded_for_cannot_open_fresh_buckets(client, monkeypatch):
    monkeypatch.setenv("BOTPPL_REG_PER_DAY", "2")
    for i in range(2):
        r = _register(client, f"agent-{i}", headers={"X-Forwarded-For": f"10.0.0.{i}"})
        assert r.status_code == 200, r.text
    r = _register(client, "agent-2", headers={"X-Forwarded-For": "10.0.0.2"})
    assert r.status_code == 429, "the header no longer changes the counted IP"
