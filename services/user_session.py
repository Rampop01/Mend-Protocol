"""
Mend — Multi-Tenant User Session & Integration Store.
Persists per-user GitHub repositories, API tokens, and NEAR testnet credentials
in SQLite (`data/switch_room.db`).
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "switch_room.db"

DEFAULT_REPO = "Rampop01/Mend"
DEFAULT_NEAR_ACCOUNT = "mend-treasury.testnet"


def init_session_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_profiles (
                chat_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                github_repo TEXT NOT NULL,
                github_token TEXT,
                near_account_id TEXT NOT NULL,
                auto_pr INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.commit()


def get_user_profile(chat_id: int, username: str = "Engineer") -> Dict[str, Any]:
    init_session_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM user_profiles WHERE chat_id = ?", (chat_id,)).fetchone()
        if row:
            return dict(row)

        now = time.time()
        conn.execute(
            """
            INSERT INTO user_profiles (chat_id, username, github_repo, github_token, near_account_id, auto_pr, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (chat_id, username, DEFAULT_REPO, None, DEFAULT_NEAR_ACCOUNT, now, now),
        )
        conn.commit()
        return {
            "chat_id": chat_id,
            "username": username,
            "github_repo": DEFAULT_REPO,
            "github_token": None,
            "near_account_id": DEFAULT_NEAR_ACCOUNT,
            "auto_pr": 1,
            "created_at": now,
            "updated_at": now,
        }


def update_github_credentials(chat_id: int, repo: str, token: Optional[str] = None) -> Dict[str, Any]:
    init_session_db()
    clean_repo = repo.strip().replace("https://github.com/", "").rstrip("/")
    now = time.time()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE user_profiles
            SET github_repo = ?, github_token = COALESCE(?, github_token), updated_at = ?
            WHERE chat_id = ?
            """,
            (clean_repo, token.strip() if token else None, now, chat_id),
        )
        conn.commit()
    return get_user_profile(chat_id)


def update_near_account(chat_id: int, near_account_id: str) -> Dict[str, Any]:
    init_session_db()
    clean_near = near_account_id.strip().lower()
    if not clean_near.endswith(".testnet") and not clean_near.endswith(".near"):
        clean_near = f"{clean_near}.testnet"
    now = time.time()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE user_profiles
            SET near_account_id = ?, updated_at = ?
            WHERE chat_id = ?
            """,
            (clean_near, now, chat_id),
        )
        conn.commit()
    return get_user_profile(chat_id)


def reset_user_profile_to_demo(chat_id: int) -> Dict[str, Any]:
    init_session_db()
    now = time.time()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE user_profiles
            SET github_repo = ?, github_token = NULL, near_account_id = ?, updated_at = ?
            WHERE chat_id = ?
            """,
            (DEFAULT_REPO, DEFAULT_NEAR_ACCOUNT, now, chat_id),
        )
        conn.commit()
    return get_user_profile(chat_id)
