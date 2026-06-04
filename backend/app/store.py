"""任务持久化：SQLite（Runtime 文档建议 SQLite 起步）。

只存 VideoJob 的 JSON 快照，键为 job_id。够 demo 用，可平滑换 PG。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .schemas import VideoJob

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "jobs.db"
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            updated_at REAL NOT NULL
        )"""
    )
    return conn


def save(job: VideoJob) -> None:
    job.touch()
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO jobs(id, data, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (job.id, job.model_dump_json(), job.updated_at),
        )


def get(job_id: str) -> Optional[VideoJob]:
    with _lock, _conn() as conn:
        row = conn.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    return VideoJob.model_validate(json.loads(row[0]))


def list_recent(limit: int = 50) -> list[VideoJob]:
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT data FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [VideoJob.model_validate(json.loads(r[0])) for r in rows]
