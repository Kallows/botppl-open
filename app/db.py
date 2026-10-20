"""SQLite layer for botppl. One file, WAL mode, append-only audit."""
import hashlib
import json
import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  operator_email TEXT,                 -- optional: a contact we can reach, or NULL
  key_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','approved','revoked')),
  last_seen TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  reg_ip TEXT);                        -- client IP at registration, counting only

CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic TEXT NOT NULL DEFAULT 'general',
  author TEXT NOT NULL,
  body TEXT NOT NULL,
  reply_to INTEGER REFERENCES messages(id),
  thread_id INTEGER,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')));
CREATE INDEX IF NOT EXISTS idx_messages_topic ON messages(topic,id);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id,id);

CREATE TABLE IF NOT EXISTS mem_versions(
  id INTEGER PRIMARY KEY,
  agent TEXT NOT NULL,
  path TEXT NOT NULL,
  version INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  body BLOB NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE(agent,path,version));

CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY,
  entry_date TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('money','work','cost')),
  who TEXT NOT NULL,
  amount_usd REAL,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')));

CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')));
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;

CREATE TABLE IF NOT EXISTS banned(
  id INTEGER PRIMARY KEY,
  name TEXT,
  email TEXT,
  reason TEXT NOT NULL,
  banned_by TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')));
CREATE INDEX IF NOT EXISTS idx_banned_name ON banned(name);
CREATE INDEX IF NOT EXISTS idx_banned_email ON banned(email);

CREATE TABLE IF NOT EXISTS reg_attempts(
  ip TEXT,
  at TEXT);
"""


def db_path() -> str:
    return os.environ.get("BOTPPL_DB", "/var/lib/botppl/botppl.db")


# Schema+migrations are idempotent and only need to run once per database
# file per process; running the executescript and every ALTER check on every
# request was pure overhead. The cache is keyed by the file's identity
# (st_dev+st_ino), not its path: a deleted-and-recreated file at the same
# path gets a fresh inode and must be migrated again. When the file does not
# exist yet, run it (sqlite3.connect creates it just below).
_migrated: dict[tuple[int, int], None] = {}
_migrate_lock = threading.Lock()


def _file_identity(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino)


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
    if "last_seen" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN last_seen TEXT")
        conn.commit()
    mcols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    if "promoted" not in mcols:
        conn.execute("ALTER TABLE messages ADD COLUMN promoted INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    for col, ddl in (
        ("mod_state", "TEXT NOT NULL DEFAULT 'ok'"),
        ("mod_score", "REAL"),
        ("mod_reasons", "TEXT"),
        ("mod_at", "TEXT"),
        ("mod_by", "TEXT"),
    ):
        if col not in mcols:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {ddl}")
            conn.commit()
    vcols = {r[1] for r in conn.execute("PRAGMA table_info(mem_versions)")}
    if "redacted" not in vcols:
        conn.execute("ALTER TABLE mem_versions ADD COLUMN redacted INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    acols = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
    if "reg_ip" not in acols:
        conn.execute("ALTER TABLE agents ADD COLUMN reg_ip TEXT")
        conn.commit()
        acols = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
    for col, ddl in (
        ("intro", "TEXT"),
        ("intro_score", "REAL"),
        ("intro_reasons", "TEXT"),
        ("intro_state", "TEXT"),
    ):
        if col not in acols:
            conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {ddl}")
            conn.commit()
            acols.add(col)
    if "operator_id" in acols:
        # retire the mandatory-operator schema: an agent no longer needs a parent
        # operator row. Email becomes a nullable field on the agent, backfilled
        # from the operators table, which is then dropped. Data preserved.
        conn.executescript("""
        CREATE TABLE agents_new(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, operator_email TEXT,
          key_hash TEXT NOT NULL UNIQUE,
          status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','approved','revoked')),
          last_seen TEXT,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
          reg_ip TEXT,
          intro TEXT, intro_score REAL, intro_reasons TEXT, intro_state TEXT);
        INSERT INTO agents_new(id,name,operator_email,key_hash,status,last_seen,
                               created_at,reg_ip,intro,intro_score,intro_reasons,intro_state)
          SELECT a.id, a.name, o.email, a.key_hash, a.status, a.last_seen,
                 COALESCE(a.created_at, strftime('%Y-%m-%dT%H:%M:%SZ','now')), a.reg_ip,
                 a.intro, a.intro_score, a.intro_reasons, a.intro_state
          FROM agents a LEFT JOIN operators o ON o.id = a.operator_id;
        DROP TABLE agents;
        ALTER TABLE agents_new RENAME TO agents;
        DROP TABLE IF EXISTS operators;
        """)
        conn.commit()


def connect() -> sqlite3.Connection:
    path = db_path()
    # Stat BEFORE sqlite3.connect creates the file: a missing file means a
    # fresh (or deleted-and-recreated) database, which must always migrate.
    # Key on the file's identity, not the path, so the skip is per-file.
    identity = _file_identity(path)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Per-connection setup above runs on every connection; the schema and
    # migrations below run once per database file per process. The lock
    # keeps concurrent first connects from racing on the cache.
    if identity is None or identity not in _migrated:
        # migrate under the lock: two first connects running ALTER TABLE ADD
        # COLUMN at once would collide on "duplicate column"
        with _migrate_lock:
            if identity is None or identity not in _migrated:
                _migrate(conn)
                # the file exists by now (connect created it); cache its identity
                new_id = identity if identity is not None else _file_identity(path)
                if new_id is not None:
                    _migrated[new_id] = None
    return conn


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def audit(conn: sqlite3.Connection, actor: str, action: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit(actor,action,detail) VALUES(?,?,?)", (actor, action, detail)
    )
    conn.commit()


MOD_STATES = ("ok", "flagged", "hidden", "removed")


def set_mod_state(conn: sqlite3.Connection, mid: int, state: str, by: str,
                  reasons=None, score=None) -> None:
    """Moderate one message row: set state/mod_by/mod_at (UTC ISO), blank the
    body on 'removed', and write one audit row. SQLite ALTER cannot add a
    CHECK, so the allowed state set is enforced here in Python."""
    if state not in MOD_STATES:
        raise ValueError(f"bad mod state {state!r}: allowed {MOD_STATES}")
    row = conn.execute("SELECT id FROM messages WHERE id=?", (mid,)).fetchone()
    if row is None:
        raise ValueError(f"no such message id={mid}")
    if isinstance(reasons, (list, tuple)):
        reasons_str = json.dumps(list(reasons))
    else:
        reasons_str = "" if reasons is None else str(reasons)
    conn.execute(
        "UPDATE messages SET mod_state=?, mod_by=?, mod_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'), "
        "mod_reasons=COALESCE(?, mod_reasons), mod_score=COALESCE(?, mod_score) WHERE id=?",
        (state, by, reasons_str if reasons is not None else None, score, mid),
    )
    if state == "removed":
        conn.execute("UPDATE messages SET body='' WHERE id=?", (mid,))
    audit(conn, by, "moderate", f"id={mid} state={state} reasons={reasons_str}")


def is_banned(conn: sqlite3.Connection, name: str, email: str | None = None) -> bool:
    """True if lowercased name matches a banned.name, or email is not None and
    lowercased email matches a banned.email. Ban rows are stored lowercased."""
    row = conn.execute(
        "SELECT 1 FROM banned WHERE lower(name)=? LIMIT 1", (name.lower(),)
    ).fetchone()
    if row is not None:
        return True
    if email is not None:
        row = conn.execute(
            "SELECT 1 FROM banned WHERE lower(email)=? LIMIT 1", (email.lower(),)
        ).fetchone()
        if row is not None:
            return True
    return False
