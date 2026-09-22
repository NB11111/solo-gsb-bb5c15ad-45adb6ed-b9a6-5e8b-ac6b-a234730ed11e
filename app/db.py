"""SQLite 任务持久化（单文件，WAL 模式）。"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DB_PATH = os.environ.get("OCR_DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "tasks.db"))

_conn: sqlite3.Connection | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> None:
    global _conn
    Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA busy_timeout=5000")
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id          TEXT PRIMARY KEY,
            status      TEXT NOT NULL,
            request     TEXT NOT NULL,
            result      TEXT,
            error       TEXT,
            created_at  TEXT NOT NULL,
            finished_at TEXT
        )
        """
    )
    _conn.commit()


def create_task(task_id: str, request: dict[str, Any]) -> None:
    assert _conn is not None
    _conn.execute(
        "INSERT INTO tasks (id, status, request, created_at) VALUES (?, ?, ?, ?)",
        (task_id, "pending", json.dumps(request, ensure_ascii=False), _now()),
    )
    _conn.commit()


def finish_task(task_id: str, result: dict[str, Any]) -> None:
    assert _conn is not None
    _conn.execute(
        "UPDATE tasks SET status=?, result=?, finished_at=? WHERE id=?",
        ("done", json.dumps(result, ensure_ascii=False), _now(), task_id),
    )
    _conn.commit()


def fail_task(task_id: str, error: str) -> None:
    assert _conn is not None
    _conn.execute(
        "UPDATE tasks SET status=?, error=?, finished_at=? WHERE id=?",
        ("failed", error, _now(), task_id),
    )
    _conn.commit()


def get_task(task_id: str) -> dict[str, Any] | None:
    assert _conn is not None
    row = _conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return None
    return {
        "task_id": row["id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "result": json.loads(row["result"]) if row["result"] else None,
        "error": row["error"],
    }
