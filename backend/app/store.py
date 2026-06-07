"""任务持久化：SQLite（Runtime 文档建议 SQLite 起步）。

存两类 job：
- VideoJob：v1.1 视频生产流水线
- ProductJob：v2.0 选品 Agent 流水线
表里加 kind 字段区分，避免互相干扰。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .schemas import ProductJob, VideoJob

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
    # 向后兼容：老库可能没有 kind 列，缺则补
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "kind" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'video'")
    return conn


def save(job: VideoJob) -> None:
    job.touch()
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO jobs(id, data, updated_at, kind) VALUES(?,?,?,'video') "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data, "
            "updated_at=excluded.updated_at, kind='video'",
            (job.id, job.model_dump_json(), job.updated_at),
        )


def get(job_id: str) -> Optional[VideoJob]:
    with _lock, _conn() as conn:
        row = conn.execute(
            "SELECT data FROM jobs WHERE id=? AND kind='video'", (job_id,)
        ).fetchone()
    if not row:
        return None
    return VideoJob.model_validate(json.loads(row[0]))


def list_recent(limit: int = 50) -> list[VideoJob]:
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT data FROM jobs WHERE kind='video' "
            "ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [VideoJob.model_validate(json.loads(r[0])) for r in rows]


# ---------- 选品 Agent v2.0 ----------
def save_product(job: ProductJob) -> None:
    job.touch()
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO jobs(id, data, updated_at, kind) VALUES(?,?,?,'product') "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data, "
            "updated_at=excluded.updated_at, kind='product'",
            (job.id, job.model_dump_json(), job.updated_at),
        )


def get_product(job_id: str) -> Optional[ProductJob]:
    with _lock, _conn() as conn:
        row = conn.execute(
            "SELECT data FROM jobs WHERE id=? AND kind='product'", (job_id,)
        ).fetchone()
    if not row:
        return None
    return ProductJob.model_validate(json.loads(row[0]))


def list_recent_products(limit: int = 50) -> list[ProductJob]:
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT data FROM jobs WHERE kind='product' "
            "ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [ProductJob.model_validate(json.loads(r[0])) for r in rows]
