from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .config import DB_PATH

# Pipeline order: new -> scored -> tailored -> applied -> (interviewing | rejected | offer)
STATUSES = [
    "new", "scored", "skipped", "tailored", "applied",
    "interviewing", "rejected", "offer", "withdrawn", "error",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,            -- <source>:<company>:<external id>
    source TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    url TEXT,
    apply_url TEXT,
    description TEXT,
    posted_at TEXT,
    fetched_at TEXT,
    score INTEGER,
    score_detail TEXT,              -- JSON from the scorer
    status TEXT NOT NULL DEFAULT 'new',
    resume_path TEXT,
    cover_path TEXT,
    applied_at TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    at TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def insert_job(conn: sqlite3.Connection, job: dict) -> bool:
    """Insert a job if unseen. Returns True when it was new."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO jobs
           (id, source, company, title, location, url, apply_url, description, posted_at, fetched_at)
           VALUES (:id, :source, :company, :title, :location, :url, :apply_url, :description, :posted_at, :fetched_at)""",
        {**job, "fetched_at": now()},
    )
    if cur.rowcount:
        log_event(conn, job["id"], "new")
    conn.commit()
    return bool(cur.rowcount)


def get(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:  # allow a unique prefix / suffix of the id
        rows = conn.execute("SELECT * FROM jobs WHERE id LIKE ?", (f"%{job_id}%",)).fetchall()
        row = rows[0] if len(rows) == 1 else None
    return row


def by_status(conn: sqlite3.Connection, status: str, order_by_score: bool = True, limit: int | None = None):
    sql = "SELECT * FROM jobs WHERE status = ?"
    if order_by_score:
        sql += " ORDER BY score DESC NULLS LAST, fetched_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, (status,)).fetchall()


def update(conn: sqlite3.Connection, job_id: str, status: str | None = None, note: str | None = None, **fields) -> None:
    if "score_detail" in fields and not isinstance(fields["score_detail"], str):
        fields["score_detail"] = json.dumps(fields["score_detail"])
    if status:
        if status not in STATUSES:
            raise ValueError(f"Unknown status {status!r}. Use one of: {', '.join(STATUSES)}")
        fields["status"] = status
    if note:
        fields["notes"] = note
    if fields:
        cols = ", ".join(f"{k} = :{k}" for k in fields)
        conn.execute(f"UPDATE jobs SET {cols} WHERE id = :_id", {**fields, "_id": job_id})
    if status:
        log_event(conn, job_id, status, note)
    conn.commit()


def log_event(conn: sqlite3.Connection, job_id: str, status: str, note: str | None = None) -> None:
    conn.execute("INSERT INTO events (job_id, at, status, note) VALUES (?, ?, ?, ?)", (job_id, now(), status, note))


def applied_today(conn: sqlite3.Connection) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    return conn.execute("SELECT COUNT(*) FROM jobs WHERE applied_at LIKE ?", (f"{today}%",)).fetchone()[0]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
