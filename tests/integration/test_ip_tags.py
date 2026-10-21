"""Keyed network tags end-to-end (task #24 phase A, Kevin 2026-10-21;
round-2 review fixes from progress/review24-round1.md, tests named r2_*).

Case table:
  T2 register+post+read from 203.0.113.7 | no "203.0.113.7" in db bytes (+wal),
                                          no reg_ip/ip columns, and not in the
                                          Python log records caplog captured
                                          (F8: caplog sees only the app's own
                                          logging calls -- uvicorn's access
                                          logger never runs under TestClient;
                                          T8's service-file check is the
                                          control for --no-access-log);
                                          NEGATIVE CONTROL: the agent's tag IS
                                          found in the db bytes
  T3 POST /api/admin/block {"agent":X}   | register/read/vault GET from X's
                                          address -> 403 "blocked"; 198.51.100.9
                                          still works; unblock -> 200 again;
                                          expired block (expires_at written
                                          directly in the past) does not block;
                                          bad admin token -> 401 and nothing
                                          blocked; audit line has the tag and
                                          never the IP
  T4 old-schema db with reg_ip (500+    | startup backfills reg_tag, drops raw
        reg_attempts rows and a crowd     | columns, empties reg_attempts; after
        of agents holding raw IPs, so     | startup ALONE (no test-side VACUUM,
        the migration frees real pages)   | F2) the db file and its -wal hold no
                                          | raw IP and PRAGMA freelist_count is
                                          | 0 -- the arbiter review24-round2
                                          | finding 1 asked for: on this build
                                          | (secure_delete compiled in) the byte
                                          | scan alone cannot go red; second
                                          | startup is a no-op
  T9 GET /board with bad X-Admin-Token   | 401 "bad admin token", detail has no
                                          "register"; valid X-Key and no admin
                                          token -> unchanged (200)
  r2_f1 old-schema db whose audit holds  | startup rewrites 'ip=<addr>' to
        register rows with ip=           | 'tag=<ip_tag(addr)>' in every audit
        (one IPv4, one IPv6) + a post    | detail whatever the action (rows
        row with ip= + a ban row whose   | kept, order kept), leaves rows with
        'ip=' has no address + a         | no ip= token byte-identical (the
        register row without ip=         | ip=-less register row and the ban
                                          | row), and appends exactly
                                          | one action='migrate-ip-tags' row
                                          | naming counts only; after startup
                                          | alone (no test-side VACUUM,
                                          | review24-round2 finding 2)
                                          | b'192.0.2.5' is absent from the file
                                          | bytes and from audit; second startup
                                          | changes nothing (idempotent, no
                                          | second migrate row); the append-only
                                          | update guard is back afterwards.
                                          | F5: 3 agent rows migrate via
                                          | fetchall-before-UPDATE
  H1 old-schema db whose audit holds     | startup rewrites the ip= token of
        register 'agent=a ip=192.0.2.5', | EVERY audit row whatever the action
        two register-ratelimited rows    | -- the live defect: the startup
        'ip=198.51.100.9' and            | migration named only
        'ip=2001:db8::1' (pre-#24        | action='register', so an old
        writes), plus negative controls  | action='register-ratelimited' row
        'ship=1.2.3.4 zip=90210' and     | kept a raw IP in the live DB. The
        'agent=b no address here'        | ratelimited rows now read
                                          | 'tag=<32 hex>'; NEGATIVE CONTROLS
                                          | 'ship=1.2.3.4 zip=90210' (words
                                          | ending in 'ip' are no token) and
                                          | 'agent=b no address here' stay
                                          | byte-identical; exactly one new
                                          | action='migrate-ip-tags' row
                                          | (counts only, never an IP); no
                                          | b'192.0.2.5', b'198.51.100.9' or
                                          | b'2001:db8::1' anywhere in the db
                                          | file or -wal bytes after startup
                                          | alone (no test-side VACUUM)
  H2 NEW-schema db (no reg_ip column,   | startup rewrites the leftover row
        the migration ran once and       | to 'tag=<32 hex>' and appends one
        missed one) holding a leftover   | more action='migrate-ip-tags' row
        action='register-ratelimited'    | (the earlier run's row stays
        row 'ip=203.0.113.7'             | byte-identical); the raw bytes are
                                          | gone from the file after startup
                                          | alone (no test-side VACUUM); a
                                          | further startup adds no row and
                                          | changes nothing
  r2_f2 connections the app opens        | PRAGMA secure_delete is ON, asserted
                                          | through connections the test hands
                                          | out already at OFF -- on this build
                                          | (secure_delete compiled in) reading1
                                          | on a fresh connection proves nothing
                                          | (review24-round2 finding 1); T4
                                          | above proves the app-side
                                          | checkpoint+VACUUM
  r2_f3 blocked tag calling the admin    | /api/admin/* is exempt from
        API                              | BlockMiddleware (token-gated): the
                                          | blocked address unblocks its own
                                          | tag -> 200; NEGATIVE CONTROL: the
                                          | same tag still gets 403 on
                                          | /api/register while blocked
  r2_f4 block validation                 | raw IP tag -> 422 and nothing in
                                          | blocked/audit; ' ' -> 422;
                                          | 'unknown' via tag -> 422; agent
                                          | whose reg_tag is 'unknown' -> 409
                                          | naming the peer-address hazard; a
                                          | valid 32-hex tag -> 200
  r2_f7 expired block                    | a row with expires_at in the past is
                                          | not listed by /api/admin/blocks and
                                          | not in the in-memory set after the
                                          | refresh (pruned there too); a live
                                          | block is listed and blocks
  r3_f1 freelist, no pending migration   | a db whose freelist is populated but
                                          | that needs no migration (an earlier
                                          | run died between commit and VACUUM)
                                          | leaves freelist_count 0 after
                                          | startup alone, and the VACUUM spy
                                          | saw the reclaim run
  r3_f2 freelist 0 (negative control)    | a clean db is NOT VACUUMed at
                                          | startup: the spy sees no VACUUM
                                          | (review24-round2 finding 3)
  r3_f4 block audit records expiry       | days=1 -> detail holds
                                          | expires_at=<the ISO the API
                                          | returned>; permanent block ->
                                          | expires_at=never
                                          | (review24-round2 finding 4)
"""
import logging
import re
import sqlite3

import pytest

from app import db
from app.main import ip_tag
from tests.integration.test_moderation import ADMIN, CLEAN_INTRO

CLIENT_IP = "203.0.113.7"
OTHER_IP = "198.51.100.9"


def _db_bytes():
    """The SQLite database file and its -wal, if present."""
    path = db.db_path()
    out = []
    with open(path, "rb") as f:
        out.append(f.read())
    for suffix in ("-wal", "-shm"):
        try:
            with open(path + suffix, "rb") as f:
                out.append(f.read())
        except FileNotFoundError:
            pass
    return b"".join(out)


def _cols(table):
    c = db.connect()
    try:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    finally:
        c.close()


def _vacuum_spy(monkeypatch):
    """Every SQL statement run through sqlite3.connect, collected via the
    connection's trace callback. The startup checkpoint+VACUUM runs on a
    fresh connection of its own (app.main.backfill_ip_tags), so this sees it
    -- and lets a test assert that VACUUM did NOT run, which a file mtime
    cannot (review24-round2 finding 3)."""
    seen: list = []
    real = sqlite3.connect

    def spy(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", spy)
    return seen


# ── T2: the raw address never lands anywhere ──────────────────────────

def test_no_raw_ip_in_db_logs_or_schema(env, caplog):
    from fastapi.testclient import TestClient

    from app.main import app

    with caplog.at_level(logging.DEBUG):
        with TestClient(app, client=(CLIENT_IP, 50000)) as client:
            r = client.post("/api/register",
                            json={"agent_name": "net-a", "intro": CLEAN_INTRO})
            assert r.status_code == 200, r.text
            key = {"X-Key": r.json()["key"]}
            assert client.post("/api/post",
                               json={"topic": "general", "body": "hello tags"},
                               headers=key).status_code == 200
            assert client.get("/api/read?topic=general&since=0",
                              headers=key).status_code == 200

    blob = _db_bytes()
    assert CLIENT_IP.encode() not in blob, "raw IP found in the database file"
    assert CLIENT_IP not in caplog.text, \
        "raw IP found in the captured Python log records"
    assert "reg_ip" not in _cols("agents")
    assert "ip" not in _cols("reg_attempts")
    # NEGATIVE CONTROL: the same byte scan CAN see stored strings -- the
    # agent's keyed tag is in there
    tag = ip_tag(CLIENT_IP)
    assert tag.encode() in blob, "scan cannot see what is actually stored"


# ── T3: blocks reject the tag, not an address book ────────────────────

def test_admin_block_unblock_and_expiry(env):
    from fastapi.testclient import TestClient

    from app.main import app, load_blocked

    victim = TestClient(app, client=(CLIENT_IP, 50000))
    other = TestClient(app, client=(OTHER_IP, 50000))
    with victim:
        r = victim.post("/api/register",
                        json={"agent_name": "block-me", "intro": CLEAN_INTRO})
        assert r.status_code == 200, r.text
        key = {"X-Key": r.json()["key"]}
        assert victim.get("/api/read?topic=general&since=0",
                          headers=key).status_code == 200

        r = other.post("/api/admin/block",
                       json={"agent": "block-me", "reason": "spam"}, headers=ADMIN)
        assert r.status_code == 200, r.text
        tag = r.json()["tag"]
        assert tag == ip_tag(CLIENT_IP)

        # every route from X's address answers 403 "blocked"
        for call in (
            lambda: victim.post("/api/register",
                                json={"agent_name": "sneaky", "intro": CLEAN_INTRO}),
            lambda: victim.get("/api/read?topic=general&since=0", headers=key),
            lambda: victim.get("/api/mem/SOUL.md", headers=key),
        ):
            r = call()
            assert r.status_code == 403, r.text
            assert r.json() == {"detail": "blocked"}

        # a request from another network still works
        r = other.post("/api/register",
                       json={"agent_name": "bystander", "intro": CLEAN_INTRO})
        assert r.status_code == 200, r.text

        # unblock -> 200 again
        r = other.post("/api/admin/unblock", json={"tag": tag}, headers=ADMIN)
        assert r.status_code == 200, r.text
        r = victim.get("/api/read?topic=general&since=0", headers=key)
        assert r.status_code == 200, r.text

    # bad admin token -> 401 and nothing blocked
    r = other.post("/api/admin/block",
                   json={"agent": "bystander", "reason": "nope"},
                   headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 401, r.text
    with victim:
        r = victim.post("/api/register",
                        json={"agent_name": "after-fail", "intro": CLEAN_INTRO})
        assert r.status_code == 200, r.text

    # an expired block does not block: days=1 through the API, then the
    # expiry written directly in the past and the in-memory set refreshed
    # the way startup and every admin block/unblock refresh it
    with victim:
        r = other.post("/api/admin/block",
                       json={"agent": "after-fail", "reason": "spam", "days": 1},
                       headers=ADMIN)
        assert r.status_code == 200, r.text
        tag2 = r.json()["tag"]
        r = victim.get("/api/read?topic=general&since=0", headers=key)
        assert r.status_code == 403, "a live day-1 block must block"
        c = db.connect()
        c.execute("UPDATE blocked SET expires_at="
                  "strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 hour') WHERE tag=?",
                  (tag2,))
        c.commit()
        c.close()
        load_blocked()  # same refresh a startup or any block/unblock performs
        r = victim.get("/api/read?topic=general&since=0", headers=key)
        assert r.status_code == 200, "an expired block must not block"

    # audit: the block line carries the tag and never an IP
    c = db.connect()
    try:
        rows = c.execute("SELECT detail FROM audit WHERE action='block'").fetchall()
    finally:
        c.close()
    joined = "\n".join(r["detail"] for r in rows)
    assert f"tag={tag}" in joined and f"tag={tag2}" in joined
    assert CLIENT_IP not in joined and OTHER_IP not in joined


# ── T4: old-schema database migrates away from raw IPs ────────────────

OLD_SCHEMA = """
CREATE TABLE agents(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, operator_email TEXT,
  key_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending',
  last_seen TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  reg_ip TEXT,
  intro TEXT, intro_score REAL, intro_reasons TEXT, intro_state TEXT);
CREATE TABLE reg_attempts(ip TEXT, at TEXT);
INSERT INTO agents(id,name,key_hash,status,reg_ip)
  VALUES(1,'veteran','kh','approved','192.0.2.5');
INSERT INTO reg_attempts(ip,at)
  VALUES('192.0.2.5', strftime('%Y-%m-%dT%H:%M:%SZ','now'));
INSERT INTO reg_attempts(ip,at)
  VALUES('192.0.2.6', strftime('%Y-%m-%dT%H:%M:%SZ','now'));
"""


def test_startup_backfill_and_drop(env):
    from fastapi.testclient import TestClient

    from app.main import app

    # The bulk is the other half of the arbiter (review24-round2 finding 1):
    # >= 500 reg_attempts rows and a crowd of agents holding raw reg_ip, so
    # dropping the raw columns and emptying the counters really frees pages.
    # With the old 2-row fixture no page is ever freed and neither the byte
    # scan nor a freelist assert can go red on this build (secure_delete is
    # compiled in, so freed content is zeroed with or without the app).
    script = OLD_SCHEMA + "".join(
        "INSERT INTO reg_attempts(ip,at) VALUES("
        f"'198.51.100.{i % 256}', strftime('%Y-%m-%dT%H:%M:%SZ','now'));\n"
        for i in range(500))
    script += "".join(
        "INSERT INTO agents(id,name,key_hash,status,reg_ip) VALUES("
        f"{i + 2},'bulk-{i}','kh-b{i}','approved','198.51.100.{i}');\n"
        for i in range(9))
    raw = sqlite3.connect(db.db_path())
    raw.executescript(script)
    raw.commit()
    raw.close()

    with TestClient(app):
        pass  # startup runs the backfill
    c = db.connect()
    try:
        row = c.execute("SELECT reg_tag FROM agents WHERE name='veteran'").fetchone()
        n = c.execute("SELECT COUNT(*) n FROM reg_attempts").fetchone()["n"]
        freelist = c.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        c.close()
    assert row["reg_tag"] == ip_tag("192.0.2.5")
    assert n == 0, "the 24 h counters are void after the key change"
    assert "reg_ip" not in _cols("agents")
    assert "ip" not in _cols("reg_attempts")
    # the arbiter: the migration freed pages, and startup alone must have
    # reclaimed them (checkpoint+VACUUM). Disable that block and this fails
    # with freelist > 0 -- while the byte scan below still passes, which is
    # exactly why finding 1 needed this assert.
    assert freelist == 0, (
        "startup must reclaim the pages the migration freed "
        f"(freelist_count={freelist}); the checkpoint+VACUUM did not run")

    # no test-side VACUUM here on purpose (F2): the migration checkpoints and
    # vacuums itself, and the raw IPs must be gone from the db file AND the
    # -wal after startup alone
    blob = _db_bytes()
    assert b"192.0.2.5" not in blob, "raw IP survived in the db file/-wal"
    assert b"192.0.2." not in blob, "raw IP family survived in the db file/-wal"
    assert b"198.51.100." not in blob, "bulk raw IPs survived in the db file/-wal"

    # startup again: no error, no change
    with TestClient(app):
        pass
    c = db.connect()
    try:
        row2 = c.execute("SELECT reg_tag FROM agents WHERE name='veteran'").fetchone()
        n2 = c.execute("SELECT COUNT(*) n FROM reg_attempts").fetchone()["n"]
    finally:
        c.close()
    assert row2["reg_tag"] == row["reg_tag"]
    assert n2 == 0
    assert "reg_ip" not in _cols("agents")
    assert "ip" not in _cols("reg_attempts")


# ── T9: a bad admin token on /board is 401, not an agent auth fallthrough ─

def test_board_bad_admin_token_is_401(env):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.get("/board?topic=general",
                   headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 401, r.text
    assert "bad admin token" in r.text
    assert "register" not in r.text.lower(), "401 must not point at registration"

    r = client.post("/api/register",
                    json={"agent_name": "board-a", "intro": CLEAN_INTRO})
    key = {"X-Key": r.json()["key"]}
    r = client.get("/board?topic=general", headers=key)
    assert r.status_code == 200, r.text
    assert "/general" in r.text


# ── r2_f1: old register audit lines are rewritten to tags in place ─────

R2_OLD_SCHEMA = """
CREATE TABLE agents(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, operator_email TEXT,
  key_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending',
  last_seen TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  reg_ip TEXT,
  intro TEXT, intro_score REAL, intro_reasons TEXT, intro_state TEXT);
CREATE TABLE reg_attempts(ip TEXT, at TEXT);
CREATE TABLE audit(
  id INTEGER PRIMARY KEY,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')));
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
INSERT INTO agents(id,name,key_hash,status,reg_ip)
  VALUES(1,'veteran-a','kh-ra','approved','192.0.2.5');
INSERT INTO agents(id,name,key_hash,status,reg_ip)
  VALUES(2,'veteran-b','kh-rb','approved','2001:db8::5');
INSERT INTO agents(id,name,key_hash,status,reg_ip)
  VALUES(3,'veteran-c','kh-rc','approved',NULL);
INSERT INTO reg_attempts(ip,at)
  VALUES('192.0.2.5', strftime('%Y-%m-%dT%H:%M:%SZ','now'));
INSERT INTO audit(id,actor,action,detail) VALUES(1,'system','register',
  'agent=veteran-a email=x ip=192.0.2.5 intro_state=ok score=1.0');
INSERT INTO audit(id,actor,action,detail) VALUES(2,'system','register',
  'agent=veteran-b email=y ip=2001:db8::5 intro_state=ok score=2.0');
INSERT INTO audit(id,actor,action,detail) VALUES(3,'system','register',
  'agent=veteran-c email=(none) intro_state=ok score=3.0');
INSERT INTO audit(id,actor,action,detail) VALUES(4,'system','post',
  'veteran-a quoted ip=198.51.100.9 from a config file');
INSERT INTO audit(id,actor,action,detail) VALUES(5,'admin','ban',
  'banned veteran-a: the older log still names ip= right here');
"""


def _audit_rows(conn):
    return [(r[0], r[1], r[2], r[3]) for r in conn.execute(
        "SELECT id, actor, action, detail FROM audit ORDER BY id")]


def test_r2_f1_register_audit_rows_rewritten_to_tags(env):
    from fastapi.testclient import TestClient

    from app.main import app

    raw = sqlite3.connect(db.db_path())
    raw.executescript(R2_OLD_SCHEMA)
    raw.commit()
    before = _audit_rows(raw)
    raw.close()
    assert len(before) == 5

    with TestClient(app):
        pass  # startup runs the migration
    raw = sqlite3.connect(db.db_path())
    after = _audit_rows(raw)
    raw.close()

    # rows kept and in order: the same five ids first, exactly one new row
    assert [r[0] for r in after[:5]] == [r[0] for r in before], \
        "audit rows were deleted or reordered"
    assert len(after) == len(before) + 1, "audit must grow by exactly one row"
    by_id = {r[0]: r for r in after}
    # every row's ip= token is rewritten to the keyed tag, whatever the
    # action (row 4 is action='post' and must be rewritten too -- the live
    # defect was an action='register-ratelimited' row the old filter missed)
    assert by_id[1][3] == (
        f"agent=veteran-a email=x tag={ip_tag('192.0.2.5')} "
        "intro_state=ok score=1.0")
    assert by_id[2][3] == (
        f"agent=veteran-b email=y tag={ip_tag('2001:db8::5')} "
        "intro_state=ok score=2.0")
    assert by_id[4][3] == (
        f"veteran-a quoted tag={ip_tag('198.51.100.9')} from a config file")
    # NEGATIVE CONTROL: a register row with no ip= part is untouched
    assert by_id[3] == before[2]
    # NEGATIVE CONTROL: 'ip=' with no address after it is not a token, so
    # the ban row is byte-identical
    assert by_id[5] == before[4]

    # the one appended row names counts only, never an address (3 rows held
    # a token: the two register rows and the post row)
    assert after[-1][2] == "migrate-ip-tags"
    detail = after[-1][3]
    assert "agents=2" in detail and "audit_rows=3" in detail
    assert "reg_attempts=1" in detail
    assert "192.0.2.5" not in detail and "2001:db8" not in detail
    assert "ip=" not in detail

    # the raw addresses are gone from audit entirely
    joined = "\n".join(r[3] for r in after)
    assert "192.0.2.5" not in joined and "2001:db8::5" not in joined

    # no test-side VACUUM here (review24-round2 finding 2): the scan must
    # pass on app startup alone -- F1 rewrites in place and the startup
    # secure_delete/checkpoint/VACUUM scrub the old row images
    blob = _db_bytes()
    assert b"192.0.2.5" not in blob, "raw IP survived in the file bytes"
    assert b"2001:db8::5" not in blob, "raw IPv6 survived in the file bytes"

    # the append-only update guard is back in place after the rewrite
    raw = sqlite3.connect(db.db_path())
    try:
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("UPDATE audit SET detail='x' WHERE id=1")
    finally:
        raw.close()

    # second startup: nothing changes, no second migrate row
    with TestClient(app):
        pass
    raw = sqlite3.connect(db.db_path())
    after2 = _audit_rows(raw)
    raw.close()
    assert after2 == after, "second startup must change nothing"
    assert len([r for r in after2 if r[2] == "migrate-ip-tags"]) == 1


# ── H1: EVERY action's ip= tokens are rewritten (the live defect) ─────

H1_OLD_SCHEMA = """
CREATE TABLE agents(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, operator_email TEXT,
  key_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending',
  last_seen TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  reg_ip TEXT,
  intro TEXT, intro_score REAL, intro_reasons TEXT, intro_state TEXT);
CREATE TABLE reg_attempts(ip TEXT, at TEXT);
CREATE TABLE audit(
  id INTEGER PRIMARY KEY,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')));
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
INSERT INTO agents(id,name,key_hash,status,reg_ip)
  VALUES(1,'agent-a','kh-ha','approved','192.0.2.5');
INSERT INTO agents(id,name,key_hash,status,reg_ip)
  VALUES(2,'agent-b','kh-hb','approved',NULL);
INSERT INTO reg_attempts(ip,at)
  VALUES('192.0.2.5', strftime('%Y-%m-%dT%H:%M:%SZ','now'));
INSERT INTO audit(id,actor,action,detail) VALUES(1,'system','register',
  'agent=a ip=192.0.2.5');
INSERT INTO audit(id,actor,action,detail) VALUES(2,'system','register-ratelimited',
  'ip=198.51.100.9');
INSERT INTO audit(id,actor,action,detail) VALUES(3,'system','register-ratelimited',
  'ip=2001:db8::1');
INSERT INTO audit(id,actor,action,detail) VALUES(4,'system','post',
  'ship=1.2.3.4 zip=90210');
INSERT INTO audit(id,actor,action,detail) VALUES(5,'system','register-ratelimited',
  'agent=b no address here');
"""


def test_h1_every_action_audit_rows_rewritten(env):
    """Live defect (2026-10-21): the startup migration rewrote ip=<addr> to
    tag=<ip_tag(addr)> only for action='register' rows, so an old
    action='register-ratelimited' row written by pre-#24 code kept a raw IP
    in the live DB. Every audit row whose detail holds an 'ip=<addr>' token
    is rewritten, whatever the action; 'ship='/'zip=' (words ending in 'ip')
    and a detail with no address are not tokens and stay byte-identical."""
    from fastapi.testclient import TestClient

    from app.main import app

    raw = sqlite3.connect(db.db_path())
    raw.executescript(H1_OLD_SCHEMA)
    raw.commit()
    before = _audit_rows(raw)
    raw.close()
    assert len(before) == 5

    with TestClient(app):
        pass  # startup runs the migration
    raw = sqlite3.connect(db.db_path())
    after = _audit_rows(raw)
    raw.close()

    # rows kept and in order; exactly one new row, the migrate row
    assert [r[0] for r in after[:5]] == [r[0] for r in before], \
        "audit rows were deleted or reordered"
    assert len(after) == len(before) + 1, "audit must grow by exactly one row"
    by_id = {r[0]: r for r in after}
    # the register row AND both register-ratelimited rows carry the tag now
    assert by_id[1][3] == f"agent=a tag={ip_tag('192.0.2.5')}"
    assert by_id[2][3] == f"tag={ip_tag('198.51.100.9')}"
    assert by_id[3][3] == f"tag={ip_tag('2001:db8::1')}"
    # the ratelimited rows now read exactly 'tag=<32 lowercase hex>'
    for rid in (2, 3):
        assert re.fullmatch(r"tag=[0-9a-f]{32}", by_id[rid][3]), by_id[rid][3]
    # NEGATIVE CONTROL: 'ship='/'zip=' are words ending in 'ip', not tokens,
    # and a detail with no address has nothing to rewrite -- byte-identical
    assert by_id[4] == before[3], "'ship='/'zip=' must not be rewritten"
    assert by_id[5] == before[4], "a detail with no address must not change"

    # exactly one new migrate-ip-tags row, naming counts only
    assert after[-1][2] == "migrate-ip-tags"
    assert len([r for r in after if r[2] == "migrate-ip-tags"]) == 1
    detail = after[-1][3]
    assert "audit_rows=3" in detail, detail
    assert "ip=" not in detail, detail
    for raw_ip in ("192.0.2.5", "198.51.100.9", "2001:db8"):
        assert raw_ip not in detail, detail

    # no test-side VACUUM: startup alone must scrub the old row images from
    # the db file AND the -wal (F2)
    blob = _db_bytes()
    for raw_ip in (b"192.0.2.5", b"198.51.100.9", b"2001:db8::1"):
        assert raw_ip not in blob, f"{raw_ip!r} survived in the db file/-wal"


# ── H2: a leftover row on an already-migrated db is still rewritten ───

def test_h2_leftover_ip_row_rewritten_on_new_schema(env):
    """The live situation after the first migration ran: schema already new
    (no reg_ip column) but one action='register-ratelimited' row was missed
    and still holds a raw IP. Startup rewrites it and appends one more
    migrate-ip-tags row; a further startup finds nothing left and adds
    no row."""
    from fastapi.testclient import TestClient

    from app.main import app

    # a NEW-schema db via the app's own schema: the migration ran once
    # before (its migrate row is here) and missed the ratelimited row
    c = db.connect()
    try:
        c.execute("INSERT INTO audit(actor,action,detail) VALUES('system',"
                  "'migrate-ip-tags','agents=2 audit_rows=2 reg_attempts=1')")
        c.execute("INSERT INTO audit(actor,action,detail) VALUES('system',"
                  "'register-ratelimited','ip=203.0.113.7')")
        c.commit()
    finally:
        c.close()
    assert "reg_ip" not in _cols("agents"), "fixture must be new-schema"
    assert b"203.0.113.7" in _db_bytes(), "fixture must hold the raw IP"

    with TestClient(app):
        pass  # startup alone rewrites the leftover row
    raw = sqlite3.connect(db.db_path())
    after = _audit_rows(raw)
    raw.close()
    mig = [r for r in after if r[2] == "migrate-ip-tags"]
    # the earlier run's migrate row stays byte-identical; the leftover row
    # reads tag=<32 hex>; exactly one new migrate row was appended
    assert after[0] == (1, "system", "migrate-ip-tags",
                        "agents=2 audit_rows=2 reg_attempts=1")
    assert after[1][3] == f"tag={ip_tag(CLIENT_IP)}"
    assert re.fullmatch(r"tag=[0-9a-f]{32}", after[1][3]), after[1][3]
    assert len(mig) == 2, "this run must append exactly one migrate row"
    assert "audit_rows=1" in mig[-1][3], mig[-1][3]
    assert "ip=" not in mig[-1][3] and CLIENT_IP not in mig[-1][3]
    assert b"203.0.113.7" not in _db_bytes(), \
        "raw IP survived in the file bytes after startup alone"

    with TestClient(app):
        pass  # a further startup finds nothing left
    raw = sqlite3.connect(db.db_path())
    after2 = _audit_rows(raw)
    raw.close()
    assert after2 == after, "a further startup must change nothing"
    assert len([r for r in after2 if r[2] == "migrate-ip-tags"]) == 2


# ── r2_f2: connections the app opens zero their deleted rows ──────────

def test_r2_f2_secure_delete_on_app_connections(env, monkeypatch):
    """db.connect() must set PRAGMA secure_delete=ON. This build (SQLite
    3.45.3, SECURE_DELETE in compile_options) already defaults the pragma to
    ON, so reading 1 on a fresh connection proves nothing about the app
    (review24-round2 finding 1). Two controls make the assert falsifiable:
    (1) the pragma IS switchable OFF on this build, and (2) every connection
    is handed to the app already at OFF, so a 1 afterwards can only come
    from db.connect() turning it on -- removing that line turns this red."""
    raw = sqlite3.connect(":memory:")
    try:
        raw.execute("PRAGMA secure_delete=OFF")
        assert raw.execute("PRAGMA secure_delete").fetchone()[0] == 0, \
            "secure_delete cannot be turned OFF on this build; a reading of "\
            "1 below would be unfalsifiable"
    finally:
        raw.close()

    real = sqlite3.connect

    def off_first(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.execute("PRAGMA secure_delete=OFF")
        return conn

    monkeypatch.setattr(sqlite3, "connect", off_first)
    c = db.connect()
    try:
        on = c.execute("PRAGMA secure_delete").fetchone()[0]
    finally:
        c.close()
    assert on == 1, "db.connect() must set PRAGMA secure_delete=ON"


# ── r2_f3: a blocked network keeps the admin API (it is token-gated) ──

def test_r2_f3_admin_routes_exempt_from_block(env):
    from fastapi.testclient import TestClient

    from app.main import app

    tag = ip_tag(CLIENT_IP)
    victim = TestClient(app, client=(CLIENT_IP, 50000))
    other = TestClient(app, client=(OTHER_IP, 50000))
    with victim:
        r = other.post("/api/admin/block",
                       json={"tag": tag, "reason": "test"}, headers=ADMIN)
        assert r.status_code == 200, r.text
        # NEGATIVE CONTROL: the very same tag is still blocked elsewhere
        r = victim.post("/api/register",
                        json={"agent_name": "r2-f3", "intro": CLEAN_INTRO})
        assert r.status_code == 403, r.text
        assert r.json() == {"detail": "blocked"}
        # ...but the blocked address can unblock its own tag (200, not 403)
        r = victim.post("/api/admin/unblock", json={"tag": tag}, headers=ADMIN)
        assert r.status_code == 200, r.text
        r = victim.post("/api/register",
                        json={"agent_name": "r2-f3", "intro": CLEAN_INTRO})
        assert r.status_code == 200, r.text


# ── r2_f4: block input validation: tags are 32 hex, never 'unknown' ───

def test_r2_f4_block_tag_validation(env):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app, client=(OTHER_IP, 50000))
    with client:
        # a raw IP address, a blank and the 'unknown' sentinel are refused
        # at the model (422): none of them may reach blocked or the audit
        for bad in ("203.0.113.7", " ", "unknown"):
            r = client.post("/api/admin/block",
                            json={"tag": bad, "reason": "nope"}, headers=ADMIN)
            assert r.status_code == 422, (bad, r.text)
        # an agent whose reg_tag is 'unknown' cannot be blocked (409)
        c = db.connect()
        c.execute(
            "INSERT INTO agents(name,key_hash,status,reg_tag) "
            "VALUES('ghost','kh-r2f4','approved','unknown')")
        c.commit()
        c.close()
        r = client.post("/api/admin/block",
                        json={"agent": "ghost", "reason": "nope"}, headers=ADMIN)
        assert r.status_code == 409, r.text
        assert "unknown" in r.text and "peer address" in r.text

        # the refusals left nothing in the db or the audit trail
        c = db.connect()
        try:
            n_blocked = c.execute(
                "SELECT COUNT(*) n FROM blocked").fetchone()["n"]
            n_audit = c.execute("SELECT COUNT(*) n FROM audit").fetchone()["n"]
            named = c.execute(
                "SELECT COUNT(*) n FROM audit WHERE detail LIKE '%203.0.113.7%'"
            ).fetchone()["n"]
        finally:
            c.close()
        assert n_blocked == 0
        assert n_audit == 0
        assert named == 0

        # a valid 32-hex tag goes through
        r = client.post("/api/admin/block",
                        json={"tag": "ab" * 16, "reason": "fine"},
                        headers=ADMIN)
        assert r.status_code == 200, r.text
        assert r.json()["tag"] == "ab" * 16


# ── r2_f7: expired blocks are hidden by /api/admin/blocks and pruned ──

def test_r2_f7_expired_blocks_hidden_and_pruned(env):
    from fastapi.testclient import TestClient

    from app import main as appmain

    live, stale = "11" * 16, "22" * 16
    client = TestClient(appmain.app, client=(OTHER_IP, 50000))
    with client:
        r = client.post("/api/admin/block",
                        json={"tag": live, "reason": "live", "days": 1},
                        headers=ADMIN)
        assert r.status_code == 200, r.text
        # an expired block seeded straight into the table, as if it aged out
        c = db.connect()
        c.execute(
            "INSERT INTO blocked(tag,reason,\"by\",at,expires_at) "
            "VALUES(?,?,?,?,?)",
            (stale, "old", "admin",
             "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"))
        c.commit()
        c.close()

        # /api/admin/blocks lists the live block and hides the expired one
        r = client.get("/api/admin/blocks", headers=ADMIN)
        assert r.status_code == 200, r.text
        tags = [b["tag"] for b in r.json()["blocks"]]
        assert live in tags, "a live block must be listed"
        assert stale not in tags, "an expired block must not be listed"

        # the refresh keeps it out of the in-memory set and prunes the row
        appmain.load_blocked()
        assert live in appmain._blocked, "a live block must be in the set"
        assert stale not in appmain._blocked, "an expired block must not block"
        c = db.connect()
        try:
            n = c.execute("SELECT COUNT(*) n FROM blocked WHERE tag=?",
                          (stale,)).fetchone()["n"]
        finally:
            c.close()
        assert n == 0, "the refresh must prune expired rows"


# ── r3_f1/f2: the checkpoint+VACUUM is driven by the freelist ─────────

def test_r3_f1_freelist_reclaimed_without_migration(env, monkeypatch):
    """review24-round2 finding 3: a database whose freelist is populated but
    that needs no migration this run -- an earlier run committed and then
    died before its VACUUM -- must be reclaimed at the next startup.
    checkpoint(TRUNCATE)+VACUUM runs whenever PRAGMA freelist_count > 0,
    not only when this run did work."""
    from fastapi.testclient import TestClient

    from app.main import app

    # populate the freelist with no pending migration: a scratch table big
    # enough to span pages, then dropped (SCHEMA already migrated the file,
    # so startup has no reg_ip/ip columns to touch)
    c = db.connect()
    c.execute("CREATE TABLE scratch(x)")
    c.executemany("INSERT INTO scratch VALUES(?)", [(i,) for i in range(2000)])
    c.commit()
    c.execute("DROP TABLE scratch")
    c.commit()
    c.close()
    c = db.connect()
    try:
        before = c.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        c.close()
    assert before > 0, "fixture must leave freed pages behind"

    seen = _vacuum_spy(monkeypatch)
    with TestClient(app):
        pass  # startup alone
    c = db.connect()
    try:
        after = c.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        c.close()
    assert after == 0, (
        f"startup must reclaim the freelist (was {before}, now {after})")
    # the spy proves the reclaim really is the app's checkpoint+VACUUM and
    # not an accident of this build (positive control for r3_f2 below)
    assert any("wal_checkpoint" in s.lower() for s in seen), seen
    assert any(s.strip().upper().startswith("VACUUM") for s in seen), seen


def test_r3_f2_no_vacuum_when_freelist_empty(env, monkeypatch):
    """review24-round2 finding 3 negative control: a clean database
    (freelist 0, no pending migration) must NOT be VACUUMed at startup.
    Asserted through the statement spy: a file mtime check is not a reliable
    signal, and freelist 0 afterwards would hold either way."""
    from fastapi.testclient import TestClient

    from app.main import app

    c = db.connect()
    try:
        before = c.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        c.close()
    assert before == 0, "fixture must start with an empty freelist"

    seen = _vacuum_spy(monkeypatch)
    with TestClient(app):
        pass  # startup alone
    c = db.connect()
    try:
        after = c.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        c.close()
    assert after == 0
    assert not any(s.strip().upper().startswith("VACUUM") for s in seen), \
        f"startup must not VACUUM a clean database, saw: {seen}"
    assert not any("wal_checkpoint" in s.lower() for s in seen), seen


# ── r3_f4: the block audit line says when the block lapses ────────────

def test_r3_f4_block_audit_records_expiry(env):
    """review24-round2 finding 4: the block audit detail carries
    expires_at=<ISO> for a temporary block and expires_at=never for a
    permanent one, so a pruned expired block still has a record saying it
    was temporary and when it lapsed."""
    from fastapi.testclient import TestClient

    from app.main import app

    temp_tag, perm_tag = "33" * 16, "44" * 16
    client = TestClient(app, client=(OTHER_IP, 50000))
    with client:
        r = client.post("/api/admin/block",
                        json={"tag": temp_tag, "reason": "spam", "days": 1},
                        headers=ADMIN)
        assert r.status_code == 200, r.text
        iso = r.json()["expires_at"]
        assert iso, "days=1 must set an expiry"
        r = client.post("/api/admin/block",
                        json={"tag": perm_tag, "reason": "abuse"},
                        headers=ADMIN)
        assert r.status_code == 200, r.text
        assert r.json()["expires_at"] is None, "no days means permanent"

    c = db.connect()
    try:
        rows = c.execute(
            "SELECT detail FROM audit WHERE action='block' ORDER BY id"
        ).fetchall()
    finally:
        c.close()
    details = [r["detail"] for r in rows]
    assert f"expires_at={iso}" in details[0], details[0]
    assert "expires_at=never" in details[1], details[1]
