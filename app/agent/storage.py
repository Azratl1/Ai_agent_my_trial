# -*- coding: utf-8 -*-
"""История проверок в SQLite (или PostgreSQL при profile with-postgres)."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.agent.settings import settings

_lock = threading.Lock()


def _db_path() -> Path:
    if settings.database_url.startswith("sqlite"):
        p = settings.database_url.split("sqlite:///")[-1]
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        return Path(p)
    raise NotImplementedError("PostgreSQL включается профилем with-postgres и тем же API (см. README)")


@contextmanager
def conn():
    c = sqlite3.connect(_db_path(), timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with _lock, conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS audits(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,
            filename TEXT NOT NULL,
            doc_hash TEXT NOT NULL,
            doc_type TEXT,
            checklist TEXT,
            status TEXT NOT NULL,           -- pass | warning | fail | error
            critical_count INTEGER DEFAULT 0,
            findings_json TEXT,
            summary TEXT,
            final_path TEXT,
            report_path TEXT,
            model TEXT,
            duration_s REAL
        );
        CREATE INDEX IF NOT EXISTS idx_audits_file ON audits(filename);
        CREATE INDEX IF NOT EXISTS idx_audits_hash ON audits(doc_hash);

        CREATE TABLE IF NOT EXISTS indexed_files(
            file TEXT PRIMARY KEY,
            kind TEXT NOT NULL,             -- etalon | incoming
            doc_hash TEXT NOT NULL,
            chunks INTEGER,
            indexed_at TEXT
        );
        """)


def record_audit(*, filename: str, doc_hash: str, doc_type: str, checklist: str,
                 status: str, findings: list, summary: str, final_path: str,
                 report_path: str, model: str, duration_s: float) -> int:
    crit = sum(1 for f in findings if getattr(f, "severity", "") == "critical"
               or (isinstance(f, dict) and f.get("severity") == "critical"))
    fj = json.dumps([f.model_dump() if hasattr(f, "model_dump") else f for f in findings],
                    ensure_ascii=False)
    with conn() as c:
        cur = c.execute(
            """INSERT INTO audits(checked_at, filename, doc_hash, doc_type, checklist, status,
                                  critical_count, findings_json, summary, final_path, report_path,
                                  model, duration_s)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.utcnow().isoformat(timespec="seconds"), filename, doc_hash, doc_type,
             checklist, status, crit, fj, summary, final_path, report_path, model, duration_s))
        return cur.lastrowid


def upsert_indexed(file: str, kind: str, doc_hash: str, chunks: int) -> None:
    with conn() as c:
        c.execute("""INSERT INTO indexed_files(file, kind, doc_hash, chunks, indexed_at)
                     VALUES(?,?,?,?,?)
                     ON CONFLICT(file) DO UPDATE SET doc_hash=excluded.doc_hash,
                       chunks=excluded.chunks, indexed_at=excluded.indexed_at""",
                  (file, kind, doc_hash, chunks, datetime.utcnow().isoformat(timespec="seconds")))


def remove_indexed(file: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM indexed_files WHERE file=?", (file,))


def is_known_hash(file: str, doc_hash: str) -> bool:
    with conn() as c:
        r = c.execute("SELECT 1 FROM indexed_files WHERE file=? AND doc_hash=?",
                      (file, doc_hash)).fetchone()
        return r is not None


def last_audit(filename: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM audits WHERE filename=? ORDER BY id DESC LIMIT 1",
                      (filename,)).fetchone()
        return dict(r) if r else None


def audit_history(limit: int = 100, status: str | None = None) -> list[dict]:
    q, args = "SELECT * FROM audits", []
    if status:
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with conn() as c:
        return [dict(r) for r in c.execute(q, args)]
