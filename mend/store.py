"""SQLite persistence for incidents, so approvals survive a server restart."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

OPEN_STATUSES = ("RECEIVED", "ANALYZING", "AWAITING_APPROVAL", "APPROVING")


class IncidentStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                incident_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                data TEXT NOT NULL
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_fingerprint ON incidents (fingerprint, status)")
        self._conn.commit()

    def save(self, incident: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO incidents (incident_id, fingerprint, status, created_at, data) VALUES (?, ?, ?, ?, ?)",
                (
                    incident["incident_id"],
                    incident["fingerprint"],
                    incident["status"],
                    incident["created_at"],
                    json.dumps(incident),
                ),
            )
            self._conn.commit()

    def get(self, incident_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT data FROM incidents WHERE incident_id = ?", (incident_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def find_open(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        marks = ",".join("?" for _ in OPEN_STATUSES)
        with self._lock:
            row = self._conn.execute(
                f"SELECT data FROM incidents WHERE fingerprint = ? AND status IN ({marks}) ORDER BY created_at DESC LIMIT 1",
                (fingerprint, *OPEN_STATUSES),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def with_status(self, statuses: List[str]) -> List[Dict[str, Any]]:
        marks = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT data FROM incidents WHERE status IN ({marks}) ORDER BY created_at", tuple(statuses)
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def count(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) FROM incidents GROUP BY status").fetchall()
        return {status: n for status, n in rows}
