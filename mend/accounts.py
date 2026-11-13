"""
Telegram users of a public Mend service, and their GitHub sign-in.

GitHub access uses the OAuth device flow: the bot shows a short code, the user
enters it on github.com, and GitHub gives Mend a token limited to public
repositories (`public_repo`). Tokens are encrypted at rest with a key that
only the server holds (MEND_SECRET_KEY).
"""
from __future__ import annotations

import base64
import json
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from nacl.secret import SecretBox
from nacl.utils import random as nacl_random

from mend.integrations.github import _ssl_context

DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
SCOPE = "public_repo"


class AccountError(Exception):
    pass


@dataclass
class User:
    telegram_id: str
    chat_id: str
    username: str
    github_login: str
    created_at: float


class Vault:
    """Symmetric encryption for stored tokens."""

    def __init__(self, key_b64: str) -> None:
        raw = base64.b64decode(key_b64) if key_b64 else b""
        if len(raw) != SecretBox.KEY_SIZE:
            raise AccountError("MEND_SECRET_KEY must be 32 random bytes, base64-encoded")
        self._box = SecretBox(raw)

    @staticmethod
    def new_key() -> str:
        return base64.b64encode(nacl_random(SecretBox.KEY_SIZE)).decode()

    def seal(self, text: str) -> str:
        return base64.b64encode(self._box.encrypt(text.encode())).decode()

    def open(self, sealed: str) -> str:
        return self._box.decrypt(base64.b64decode(sealed)).decode()


class AccountStore:
    def __init__(self, db_path: Path, vault: Vault) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.vault = vault
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                username TEXT,
                github_login TEXT,
                github_token TEXT,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_logins (
                telegram_id TEXT PRIMARY KEY,
                device_code TEXT NOT NULL,
                user_code TEXT NOT NULL,
                interval INTEGER NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS usage (
                telegram_id TEXT NOT NULL,
                action TEXT NOT NULL,
                day TEXT NOT NULL,
                count INTEGER NOT NULL,
                PRIMARY KEY (telegram_id, action, day)
            );
            """
        )
        columns = [r[1] for r in self._conn.execute("PRAGMA table_info(users)")]
        if "near_account" not in columns:
            self._conn.execute("ALTER TABLE users ADD COLUMN near_account TEXT")
        self._conn.commit()

    def set_near_account(self, telegram_id: str, account_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE users SET near_account = ? WHERE telegram_id = ?", (account_id, telegram_id))
            self._conn.commit()

    def near_account(self, telegram_id: str) -> str:
        with self._lock:
            row = self._conn.execute("SELECT near_account FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return (row[0] or "") if row else ""

    def upsert(self, telegram_id: str, chat_id: str, username: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO users (telegram_id, chat_id, username, created_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(telegram_id) DO UPDATE SET chat_id = excluded.chat_id, username = excluded.username""",
                (telegram_id, chat_id, username, time.time()),
            )
            self._conn.commit()

    def get(self, telegram_id: str) -> Optional[User]:
        with self._lock:
            row = self._conn.execute(
                "SELECT telegram_id, chat_id, username, github_login, created_at FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
        return User(row[0], row[1], row[2] or "", row[3] or "", row[4]) if row else None

    def set_github(self, telegram_id: str, login: str, token: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO users (telegram_id, chat_id, github_login, github_token, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(telegram_id) DO UPDATE SET github_login = excluded.github_login,
                                                          github_token = excluded.github_token""",
                (telegram_id, telegram_id, login, self.vault.seal(token), time.time()),
            )
            self._conn.commit()

    def clear_github(self, telegram_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE users SET github_login = NULL, github_token = NULL WHERE telegram_id = ?",
                               (telegram_id,))
            self._conn.commit()

    def github_token(self, telegram_id: str) -> str:
        with self._lock:
            row = self._conn.execute("SELECT github_token FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return self.vault.open(row[0]) if row and row[0] else ""

    def take_quota(self, telegram_id: str, action: str, limit: int) -> bool:
        """Counts one use of `action` today. Returns False once the daily limit is reached."""
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self._lock:
            row = self._conn.execute("SELECT count FROM usage WHERE telegram_id = ? AND action = ? AND day = ?",
                                     (telegram_id, action, day)).fetchone()
            used = row[0] if row else 0
            if used >= limit:
                return False
            self._conn.execute(
                """INSERT INTO usage (telegram_id, action, day, count) VALUES (?, ?, ?, 1)
                   ON CONFLICT(telegram_id, action, day) DO UPDATE SET count = count + 1""",
                (telegram_id, action, day),
            )
            self._conn.commit()
        return True

    def save_pending(self, telegram_id: str, device_code: str, user_code: str, interval: int, expires_in: int) -> None:
        """Remembers a sign-in in progress, so a server restart can resume waiting for it."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO pending_logins VALUES (?, ?, ?, ?, ?)",
                (telegram_id, self.vault.seal(device_code), user_code, interval, time.time() + expires_in),
            )
            self._conn.commit()

    def pending(self, telegram_id: Optional[str] = None) -> List[Dict[str, Any]]:
        query = "SELECT telegram_id, device_code, user_code, interval, expires_at FROM pending_logins WHERE expires_at > ?"
        args: List[Any] = [time.time()]
        if telegram_id is not None:
            query += " AND telegram_id = ?"
            args.append(telegram_id)
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        return [{"telegram_id": r[0], "device_code": self.vault.open(r[1]), "user_code": r[2], "interval": r[3],
                 "expires_in": int(r[4] - time.time())} for r in rows]

    def clear_pending(self, telegram_id: str, device_code: Optional[str] = None) -> None:
        with self._lock:
            rows = self._conn.execute("SELECT device_code FROM pending_logins WHERE telegram_id = ?", (telegram_id,)).fetchall()
            if device_code is None or any(self.vault.open(r[0]) == device_code for r in rows):
                self._conn.execute("DELETE FROM pending_logins WHERE telegram_id = ?", (telegram_id,))
                self._conn.commit()

    def count_users(self) -> Dict[str, int]:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            linked = self._conn.execute("SELECT COUNT(*) FROM users WHERE github_login IS NOT NULL").fetchone()[0]
        return {"users": total, "github_connected": linked}


def _post_form(url: str, data: Dict[str, str]) -> Dict[str, Any]:
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(data).encode(),
        headers={"Accept": "application/json", "User-Agent": "mend-protocol"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode())
        except ValueError:
            return {"error": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"error": str(exc)}


class DeviceFlow:
    """GitHub's device authorization flow for an OAuth App with device flow enabled."""

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id

    def start(self) -> Dict[str, Any]:
        data = _post_form(DEVICE_CODE_URL, {"client_id": self.client_id, "scope": SCOPE})
        if "device_code" not in data:
            raise AccountError(f"GitHub refused the sign-in request: {data.get('error_description') or data.get('error')}")
        return data

    def wait(self, device_code: str, interval: int, expires_in: int,
             still_wanted: Optional[Any] = None) -> str:
        """Polls until the user approves. Returns the access token. `still_wanted()` returning False cancels."""
        deadline = time.time() + min(expires_in, 900)
        delay = max(int(interval), 5)
        while time.time() < deadline:
            time.sleep(delay)
            if still_wanted is not None and not still_wanted():
                raise AccountError("superseded")
            data = _post_form(TOKEN_URL, {
                "client_id": self.client_id, "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            })
            if data.get("access_token"):
                return data["access_token"]
            error = data.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                delay = int(data.get("interval", delay + 5))
                continue
            if error in ("expired_token", "access_denied"):
                raise AccountError("The GitHub code expired or was declined. Send /connect to try again.")
            raise AccountError(f"GitHub sign-in failed: {data.get('error_description') or error}")
        raise AccountError("The GitHub code expired. Send /connect to try again.")


def new_ingest_token() -> str:
    return "mend_" + secrets.token_urlsafe(24)
