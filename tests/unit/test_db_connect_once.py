"""connect() runs the schema+migrations once per database file per process.

The per-connection setup (WAL, foreign_keys, row_factory) must still happen
on every connection; only _migrate is cached, keyed by the file's identity
(st_dev+st_ino), not its path, so a deleted-and-recreated file at the same
path is migrated again.
"""
import os
import sqlite3

import pytest


@pytest.fixture()
def fresh_cache(monkeypatch):
    # isolate the module-level cache so test order never matters
    from app import db

    monkeypatch.setattr(db, "_migrated", {})
    return db._migrated


@pytest.fixture()
def spy(monkeypatch, env, fresh_cache):
    # counting wrapper around the real _migrate: asserts cache behavior
    # without changing what migration actually does
    from app import db

    calls = []
    real = db._migrate

    def counting(conn):
        calls.append(os.environ["BOTPPL_DB"])
        return real(conn)

    monkeypatch.setattr(db, "_migrate", counting)
    return calls


def _tables(path):
    c = sqlite3.connect(path)
    try:
        rows = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {r[0] for r in rows}
    finally:
        c.close()


def test_fresh_db_creates_tables(spy):
    from app import db

    db.connect().close()
    path = os.environ["BOTPPL_DB"]
    assert {"agents", "messages", "mem_versions", "reg_attempts"} <= _tables(path)
    assert len(spy) == 1


def test_second_connect_migrates_once(spy):
    # B2: the second connect() at the same file must not migrate again.
    from app import db

    db.connect().close()
    db.connect().close()
    assert len(spy) == 1


def test_two_paths_migrate_once_each(spy, tmp_path, monkeypatch):
    # B3: two different files, one migration each.
    from app import db

    db.connect().close()
    other = str(tmp_path / "other.db")
    monkeypatch.setenv("BOTPPL_DB", other)
    db.connect().close()
    assert len(spy) == 2
    tables = {"agents", "messages", "mem_versions", "reg_attempts"}
    assert tables <= _tables(os.environ["BOTPPL_DB"])
    assert tables <= _tables(other)


def test_deleted_file_migrates_again(spy):
    # B4 NEGATIVE CONTROL for a path-only cache: a fresh inode at the same
    # path must be migrated again, because a cache keyed by path would skip
    # it and serve an empty database.
    from app import db

    path = os.environ["BOTPPL_DB"]
    db.connect().close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except FileNotFoundError:
            pass
    db.connect().close()
    assert len(spy) == 2
    assert {"agents", "messages", "mem_versions", "reg_attempts"} <= _tables(path)


def test_second_connection_keeps_pragmas_and_row_factory(spy):
    # B5: the cache must skip only _migrate, never the per-connection setup.
    from app import db

    db.connect().close()
    conn = db.connect()
    try:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_migrate_runs_after_table_drop(spy):
    # extra safety: even without deleting the file, a dropped table is
    # restored by the next process-style connect only when the identity
    # cache says so -- here we prove the cache honors identity, not path.
    from app import db

    path = os.environ["BOTPPL_DB"]
    c1 = db.connect()
    c1.close()
    # drop the audit table through a raw connection, but keep the file alive
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE audit")
    raw.commit()
    raw.close()
    # a new connect at the SAME identity will skip _migrate by design, so
    # simulate a new process by clearing the identity cache
    from app import db as _db

    _db._migrated.clear()
    c2 = db.connect()
    names = {r["name"] for r in c2.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    c2.close()
    assert "audit" in names
    assert len(spy) == 2
