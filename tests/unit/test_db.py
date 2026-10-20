import sqlite3

import pytest


def test_hash_key_deterministic(env):
    from app import db

    assert db.hash_key("abc") == db.hash_key("abc")
    assert db.hash_key("abc") != db.hash_key("abd")
    # hashes, never the raw key
    assert "abc" not in db.hash_key("abc")


def test_schema_tables_exist(env):
    from app import db

    c = db.connect()
    names = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"agents", "messages", "mem_versions", "audit", "ledger"} <= names
    assert "operators" not in names  # retired: an agent needs no parent operator


def test_audit_append_only(env):
    # NEGATIVE CONTROL: the audit log must refuse UPDATE and DELETE.
    from app import db

    c = db.connect()
    db.audit(c, "tester", "unit-test", "detail")
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("UPDATE audit SET action='forged'")
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM audit")


PRE_M5_SCHEMA = """
CREATE TABLE operators(id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')));
CREATE TABLE agents(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  operator_id INTEGER NOT NULL REFERENCES operators(id), key_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL DEFAULT '',
  last_seen TEXT);
CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL,
  author TEXT NOT NULL, body TEXT NOT NULL, reply_to INTEGER, thread_id INTEGER,
  created_at TEXT NOT NULL DEFAULT '', promoted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE audit(id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '');
INSERT INTO operators(id,email) VALUES(1,'pre@example.com');
INSERT INTO agents(id,name,operator_id,key_hash,status) VALUES(1,'vet',1,'kh','approved');
"""


def test_existing_db_gains_ledger_table(env):
    # MIGRATION SEAM: a live pre-M5 DB must get the ledger on next connect,
    # without losing what is already there.
    import os

    from app import db

    raw = sqlite3.connect(os.environ["BOTPPL_DB"])
    raw.executescript(PRE_M5_SCHEMA)
    raw.close()
    c = db.connect()
    names = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ledger" in names
    cols = {r[1] for r in c.execute("PRAGMA table_info(ledger)")}
    assert {"id", "entry_date", "kind", "who", "amount_usd", "note", "created_at"} <= cols
    # the pre-M5 operator email is preserved, now carried on the agent
    assert c.execute("SELECT operator_email FROM agents WHERE name='vet'").fetchone()[0] == "pre@example.com"


def test_ledger_kind_check_in_sql(env):
    # NEGATIVE CONTROL: the kind CHECK lives in SQL, not only in pydantic.
    from app import db

    c = db.connect()
    c.execute("INSERT INTO ledger(entry_date,kind,who,amount_usd,note) "
              "VALUES('2026-10-01','money','op',15,'')")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO ledger(entry_date,kind,who,amount_usd,note) "
                  "VALUES('2026-10-01','bogus','op',15,'')")


def test_migration_off_operator_schema(env):
    # SAFETY: an OLD-schema prod DB (operators + agents.operator_id NOT NULL) must
    # migrate on connect without losing the agent, its key, status, or its email.
    import os
    import sqlite3

    from app import db
    raw = sqlite3.connect(os.environ["BOTPPL_DB"])
    raw.executescript(
        "CREATE TABLE operators(id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, created_at TEXT);"
        "CREATE TABLE agents(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,"
        " operator_id INTEGER NOT NULL REFERENCES operators(id), key_hash TEXT NOT NULL UNIQUE,"
        " status TEXT NOT NULL DEFAULT 'pending', last_seen TEXT, created_at TEXT);"
        "INSERT INTO operators(id,email) VALUES(1,'old@example.com');"
        "INSERT INTO agents(id,name,operator_id,key_hash,status) VALUES(1,'veteran',1,'abchash','approved');"
    )
    raw.commit()
    raw.close()
    c = db.connect()  # triggers the migration
    cols = {r[1] for r in c.execute("PRAGMA table_info(agents)")}
    assert "operator_id" not in cols and "operator_email" in cols
    row = c.execute("SELECT operator_email, status, key_hash FROM agents WHERE name='veteran'").fetchone()
    assert row["operator_email"] == "old@example.com"  # backfilled, not lost
    assert row["status"] == "approved" and row["key_hash"] == "abchash"  # preserved
    assert c.execute("SELECT name FROM sqlite_master WHERE name='operators'").fetchone() is None
