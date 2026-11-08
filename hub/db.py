"""The SQLite database: schema, the shared connection and stored settings."""

import json
import os
import secrets
import sqlite3
import threading
from pathlib import Path

from .config import BACKUP_DIR, BLOB_DIR, DB_PATH, DEFAULT_SETTINGS, SECRET_SETTINGS
from .util import now


db_lock = threading.RLock()


class Rows(list):
    """A finished query: its rows, already read."""
    rowcount = -1
    lastrowid = None

    def fetchone(self):
        return self.pop(0) if self else None

    def fetchall(self):
        return self


class SharedConnection(sqlite3.Connection):
    """One connection for every thread. Threads running the same SQL at once can step the same cached statement
    and lose or swap rows, so each statement runs to completion under the lock."""

    def execute(self, sql, parameters=()):
        with db_lock:
            cur = super().execute(sql, parameters)
            rows = Rows(cur.fetchall())
            rows.rowcount, rows.lastrowid = cur.rowcount, cur.lastrowid
            return rows


def connect():
    BLOB_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False, factory=SharedConnection)
    private_files()
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS cards (
            id TEXT PRIMARY KEY, source TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '', project TEXT NOT NULL DEFAULT '',
            col TEXT NOT NULL DEFAULT 'active', position REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', cwd TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', last_prompt TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '', msg_count INTEGER NOT NULL DEFAULT 0, user_fields TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL, updated_at REAL NOT NULL, last_activity REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, card_id TEXT NOT NULL, kind TEXT NOT NULL, at REAL NOT NULL,
            detail TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS events_card ON events(card_id, at);
        CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id TEXT NOT NULL,            -- owner: a card id, 'task:<id>' or 'project:<id>'
            kind TEXT NOT NULL,               -- handoff | doc | digest | transcript | upload
            name TEXT NOT NULL, path TEXT NOT NULL DEFAULT '', sha TEXT NOT NULL, size INTEGER NOT NULL,
            mime TEXT NOT NULL DEFAULT 'application/octet-stream', created_at REAL NOT NULL,
            UNIQUE(card_id, path, sha)
        );
        CREATE INDEX IF NOT EXISTS attachments_card ON attachments(card_id, path, created_at);
        CREATE TABLE IF NOT EXISTS files_log (
            card_id TEXT NOT NULL, path TEXT NOT NULL, first_at REAL NOT NULL, last_at REAL NOT NULL,
            edits INTEGER NOT NULL DEFAULT 1, last_action TEXT NOT NULL DEFAULT 'edit', PRIMARY KEY (card_id, path)
        );
        CREATE TABLE IF NOT EXISTS worklog (
            id INTEGER PRIMARY KEY AUTOINCREMENT, card_id TEXT NOT NULL, at REAL NOT NULL, ref TEXT NOT NULL,
            text TEXT NOT NULL, UNIQUE(card_id, ref)
        );
        CREATE INDEX IF NOT EXISTS worklog_card ON worklog(card_id, at);
        CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
            card_id UNINDEXED, kind UNINDEXED, ref UNINDEXED, title, body, tokenize='porter unicode61'
        );
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
            goals TEXT NOT NULL DEFAULT '', links TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, field TEXT NOT NULL,
            pattern TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
            notes TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS usage (
            card_id TEXT NOT NULL, model TEXT NOT NULL, input INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0,
            cache_write INTEGER NOT NULL DEFAULT 0, cache_read INTEGER NOT NULL DEFAULT 0, messages INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (card_id, model)
        );
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL,
            idx INTEGER NOT NULL, hash TEXT NOT NULL, text TEXT NOT NULL, vec BLOB, model TEXT NOT NULL DEFAULT '',
            UNIQUE(owner, kind, ref, idx)
        );
        CREATE INDEX IF NOT EXISTS chunks_owner ON chunks(owner);
        CREATE TABLE IF NOT EXISTS usage_daily (
            card_id TEXT NOT NULL, day TEXT NOT NULL, model TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
            input INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0, cache_write INTEGER NOT NULL DEFAULT 0,
            cache_read INTEGER NOT NULL DEFAULT 0, messages INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (card_id, day, model)
        );
        CREATE INDEX IF NOT EXISTS usage_daily_day ON usage_daily(day);
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, pw TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'member', created_at REAL NOT NULL, last_login REAL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS gpu_usage (
            day TEXT NOT NULL, client TEXT NOT NULL, model TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
            input INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, client, model)
        );
        CREATE TABLE IF NOT EXISTS handoff_drafts (
            project_id TEXT PRIMARY KEY, name TEXT NOT NULL, text TEXT NOT NULL, used TEXT NOT NULL DEFAULT '[]',
            excluded TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS links (
            card_id TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '', repo TEXT NOT NULL DEFAULT '', at REAL,
            PRIMARY KEY (card_id, kind, key)
        );
        """
    )
    cols = {r["name"] for r in c.execute("PRAGMA table_info(cards)")}
    for name, ddl in (("archived_at", "REAL"), ("summary", "TEXT NOT NULL DEFAULT ''"), ("source_updated", "REAL"),
                      ("source_project", "TEXT NOT NULL DEFAULT ''"), ("task_id", "TEXT"), ("notified_at", "REAL"),
                      ("deleted_at", "REAL"), ("host", "TEXT NOT NULL DEFAULT ''")):
        if name not in cols:
            c.execute(f"ALTER TABLE cards ADD COLUMN {name} {ddl}")
            if name == "source_project":
                c.execute("UPDATE cards SET source_project=project")
    for table, name, ddl in (("projects", "manual", "INTEGER NOT NULL DEFAULT 0"), ("projects", "context", "INTEGER NOT NULL DEFAULT 1"), ("tasks", "priority", "TEXT NOT NULL DEFAULT 'normal'"),
                             ("tasks", "due", "TEXT NOT NULL DEFAULT ''")):
        if name not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    for k, v in DEFAULT_SETTINGS.items():
        c.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (k, v))
    if not c.execute("SELECT 1 FROM usage_daily LIMIT 1").fetchone():
        # First run with daily history: date existing totals by last activity until a reindex gives real days.
        c.execute("""INSERT INTO usage_daily(card_id, day, model, source, input, output, cache_write, cache_read, messages)
                     SELECT u.card_id, date(COALESCE(k.last_activity, unixepoch()), 'unixepoch', 'localtime'), u.model,
                            COALESCE(k.source, substr(u.card_id, 1, instr(u.card_id, ':') - 1)),
                            u.input, u.output, u.cache_write, u.cache_read, u.messages
                     FROM usage u LEFT JOIN cards k ON k.id=u.card_id""")
    clean_stored(c)
    for r in c.execute("SELECT DISTINCT project FROM cards WHERE project != ''").fetchall():
        c.execute("INSERT OR IGNORE INTO projects(id, name, created_at, updated_at) VALUES (?,?,?,?)",
                  (secrets.token_hex(5), r["project"], now(), now()))
    c.commit()
    return c


def private_files():
    """The database and files hold full transcripts: only the hub's own user may read them."""
    for p in [Path(DB_PATH + x) for x in ("", "-wal", "-shm")] + [BLOB_DIR]:
        try:
            if p.exists():
                os.chmod(p, 0o700 if p.is_dir() else 0o600)
        except OSError:
            pass


def clean_stored(c):
    """Values stored before the hub checked them: file types that aren't bare media types, and links that aren't web addresses."""
    from .util import clean_mime, safe_url
    for r in c.execute("SELECT id, name, mime FROM attachments").fetchall():
        m = clean_mime(r["mime"], r["name"])
        if m != r["mime"]:
            c.execute("UPDATE attachments SET mime=? WHERE id=?", (m, r["id"]))
    for r in c.execute("SELECT id, url FROM cards WHERE url IS NOT NULL AND url != ''").fetchall():
        if safe_url(r["url"]) != r["url"]:
            c.execute("UPDATE cards SET url=? WHERE id=?", (safe_url(r["url"]) or None, r["id"]))
    for r in c.execute("SELECT rowid, url FROM links WHERE url != ''").fetchall():
        if safe_url(r["url"]) != r["url"]:
            c.execute("UPDATE links SET url=? WHERE rowid=?", (safe_url(r["url"]), r["rowid"]))
    for r in c.execute("SELECT id, links FROM projects WHERE links != '[]'").fetchall():
        try:
            links = json.loads(r["links"])
            ok = [l for l in links if isinstance(l, dict) and safe_url(l.get("url"))]
        except (ValueError, TypeError):
            ok, links = [], None
        if ok != links:
            c.execute("UPDATE projects SET links=? WHERE id=?", (json.dumps(ok), r["id"]))


conn = None  # set in main() / restore


def setting(key: str, default="") -> str:
    row = conn.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def int_setting(key, default):
    try:
        return int(float(setting(key, str(default))))
    except ValueError:
        return default


def set_setting(key: str, value: str):
    with db_lock:
        conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
        conn.commit()


def public_settings():
    out = {}
    for r in conn.execute("SELECT k, v FROM settings"):
        out[r["k"]] = ("set" if r["v"] else "") if r["k"] in SECRET_SETTINGS else r["v"]
    out["backup_configured"] = "true" if BACKUP_DIR else ""
    return out
