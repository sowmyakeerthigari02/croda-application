"""Database access: Supabase Postgres in production, SQLite in dev. The SQL is the same for both."""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

_lock = threading.RLock()
_sqlite: sqlite3.Connection | None = None
_pool: ConnectionPool | None = None

JSON_COLUMNS = {"profile", "job", "questions", "answers", "log"}
TABLES = ("applicants", "resumes", "saved_answers", "applications", "credentials", "mailboxes", "account_consent")

SCHEMA = """
CREATE TABLE IF NOT EXISTS applicants(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, profile TEXT NOT NULL DEFAULT '{}',
  browser_session BLOB, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resumes(
  id TEXT PRIMARY KEY, applicant_id TEXT NOT NULL REFERENCES applicants(id),
  filename TEXT NOT NULL, path TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS saved_answers(
  applicant_id TEXT NOT NULL REFERENCES applicants(id), key TEXT NOT NULL,
  question TEXT NOT NULL, answer TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(applicant_id, key));
CREATE TABLE IF NOT EXISTS applications(
  id TEXT PRIMARY KEY, applicant_id TEXT NOT NULL REFERENCES applicants(id),
  job_url TEXT NOT NULL, resume_id TEXT NOT NULL REFERENCES resumes(id),
  job TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
  current_page TEXT NOT NULL DEFAULT '', pause_kind TEXT NOT NULL DEFAULT '',
  pause_reason TEXT NOT NULL DEFAULT '', questions TEXT NOT NULL DEFAULT '[]',
  answers TEXT NOT NULL DEFAULT '{}', decision TEXT NOT NULL DEFAULT '',
  submission_status TEXT NOT NULL DEFAULT 'Not submitted', screenshot TEXT NOT NULL DEFAULT '',
  log TEXT NOT NULL DEFAULT '[]', started_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_app_applicant ON applications(applicant_id, updated_at);
CREATE TABLE IF NOT EXISTS credentials(
  id TEXT PRIMARY KEY, applicant_id TEXT NOT NULL REFERENCES applicants(id),
  site TEXT NOT NULL, login_email TEXT NOT NULL, secret BLOB NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(applicant_id, site, login_email));
CREATE TABLE IF NOT EXISTS mailboxes(
  applicant_id TEXT PRIMARY KEY REFERENCES applicants(id), address TEXT NOT NULL, secret BLOB NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS account_consent(
  applicant_id TEXT PRIMARY KEY REFERENCES applicants(id), granted_at TEXT NOT NULL);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def init() -> None:
    """Connect and create the tables if this is a new database."""
    global _sqlite, _pool
    with _lock:
        if config.DATABASE_URL:
            if _pool is None:
                # prepare_threshold=None keeps this working through Supabase's connection pooler.
                _pool = ConnectionPool(
                    config.DATABASE_URL, min_size=1, max_size=4, open=True, check=ConnectionPool.check_connection,
                    kwargs={"row_factory": dict_row, "prepare_threshold": None},
                )
                with _pool.connection() as connection:
                    for statement in SCHEMA.replace("BLOB", "BYTEA").split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    # Supabase exposes tables through a public API. With row level security on
                    # and no policies, that API sees nothing; this app connects as the owner.
                    for table in TABLES:
                        connection.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        elif _sqlite is None:
            _sqlite = sqlite3.connect(config.DB_PATH, check_same_thread=False)
            _sqlite.row_factory = sqlite3.Row
            _sqlite.execute("PRAGMA journal_mode=WAL")
            _sqlite.execute("PRAGMA foreign_keys=ON")
            _sqlite.executescript(SCHEMA)


def close() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def _run(sql: str, args: tuple, fetch: bool) -> list:
    init()
    with _lock:
        if _pool is not None:
            with _pool.connection() as connection:
                cursor = connection.execute(sql.replace("?", "%s"), args or None)
                return cursor.fetchall() if fetch else []
        cursor = _sqlite.execute(sql, args)
        rows = cursor.fetchall() if fetch else []
        if not fetch:
            _sqlite.commit()
        return rows


def _row(row) -> dict:
    out = dict(row)
    for col in JSON_COLUMNS & out.keys():
        out[col] = json.loads(out[col])
    return out


def query(sql: str, *args) -> list[dict]:
    return [_row(r) for r in _run(sql, args, fetch=True)]


def one(sql: str, *args) -> dict | None:
    rows = query(sql, *args)
    return rows[0] if rows else None


def execute(sql: str, *args) -> None:
    _run(sql, args, fetch=False)


def insert(table: str, **fields) -> None:
    cols = ",".join(fields)
    marks = ",".join("?" * len(fields))
    execute(f"INSERT INTO {table}({cols}) VALUES({marks})", *[_encode(k, v) for k, v in fields.items()])


def _encode(col, value):
    return json.dumps(value) if col in JSON_COLUMNS else value


def update_app(app_id: str, **fields) -> None:
    fields["updated_at"] = now()
    sets = ",".join(f"{k}=?" for k in fields)
    execute(f"UPDATE applications SET {sets} WHERE id=?", *[_encode(k, v) for k, v in fields.items()], app_id)


def get_app(app_id: str) -> dict | None:
    return one("SELECT * FROM applications WHERE id=?", app_id)


def add_log(app_id: str, message: str) -> None:
    with _lock:
        app = get_app(app_id)
        if app:
            update_app(app_id, log=app["log"] + [{"at": now(), "message": message}])


def saved_answers(applicant_id: str) -> dict[str, str]:
    rows = query("SELECT key, answer FROM saved_answers WHERE applicant_id=?", applicant_id)
    return {r["key"]: r["answer"] for r in rows}


def save_answer(applicant_id: str, key: str, question: str, answer: str) -> None:
    execute(
        "INSERT INTO saved_answers(applicant_id,key,question,answer,updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(applicant_id,key) DO UPDATE SET question=excluded.question, "
        "answer=excluded.answer, updated_at=excluded.updated_at",
        applicant_id, key, question, answer, now(),
    )
