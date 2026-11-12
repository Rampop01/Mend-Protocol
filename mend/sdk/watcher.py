"""
Mend SDK: reports unhandled exceptions to a Mend server.

The SDK has no dependencies. It sends the exception type and message, the
traceback, and the local variables of the last few frames with anything that
looks like a credential redacted. It never changes the behaviour of the code
it watches: the original exception is re-raised unless you ask otherwise.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import sys
import traceback
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Type

logger = logging.getLogger("mend.sdk")

DEFAULT_ENDPOINT = "http://127.0.0.1:8770/api/v1/incidents"
SENSITIVE_KEY_SUBSTRINGS = ("password", "passwd", "secret", "token", "key", "auth", "credential", "private", "cookie", "session")
FRAMES_WITH_LOCALS = 6
REDACTED = "[REDACTED]"


def sanitize_frame_locals(frame_locals: Dict[str, Any]) -> Dict[str, str]:
    """Stringifies local variables, redacting names that suggest a credential."""
    safe: Dict[str, str] = {}
    for name, value in frame_locals.items():
        if name.startswith("__"):
            continue
        if any(marker in name.lower() for marker in SENSITIVE_KEY_SUBSTRINGS):
            safe[name] = REDACTED
            continue
        try:
            text = repr(value)
        except Exception:
            text = "<unprintable>"
        safe[name] = text if len(text) <= 200 else text[:197] + "..."
    return safe


def extract_incident_context(exc: BaseException, repo: Optional[str] = None) -> Dict[str, Any]:
    """Builds the incident payload from an exception and its traceback."""
    frames: List[Dict[str, Any]] = []
    tb = exc.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        frames.append({
            "file": os.path.abspath(code.co_filename),
            "line": tb.tb_lineno,
            "function": code.co_name,
            "_locals": tb.tb_frame.f_locals,
        })
        tb = tb.tb_next
    for index, frame in enumerate(frames):
        raw = frame.pop("_locals")
        if index >= len(frames) - FRAMES_WITH_LOCALS:
            frame["locals"] = sanitize_frame_locals(raw)

    return {
        "error_type": type(exc).__name__,
        "error_message": str(exc),
        "frames": frames,
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        "repo": repo,
    }


class MendWatch:
    """
    Watches a function, a block, or the whole process.

        watch = MendWatch(repo="org/repo")

        @watch
        def handler(...): ...

        with watch:
            risky()
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        repo: Optional[str] = None,
        token: Optional[str] = None,
        raise_on_error: bool = True,
        timeout: float = 5.0,
    ) -> None:
        self.endpoint = endpoint or os.environ.get("MEND_ENDPOINT", DEFAULT_ENDPOINT)
        self.repo = repo or os.environ.get("MEND_REPO")
        self.token = token or os.environ.get("MEND_INGEST_TOKEN", "")
        self.raise_on_error = raise_on_error
        self.timeout = timeout

    def capture_exception(self, exc: BaseException) -> Optional[Dict[str, Any]]:
        """Reports an exception. Returns the server's reply, or None if it could not be delivered."""
        if os.environ.get("MEND_DISABLED"):
            return None
        headers = {"Content-Type": "application/json", "User-Agent": "mend-sdk"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            payload = json.dumps(extract_incident_context(exc, repo=self.repo)).encode("utf-8")
            req = urllib.request.Request(self.endpoint, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except Exception as error:  # reporting must never break the watched application
            logger.warning("Mend could not report the incident to %s: %s", self.endpoint, error)
            return None

    def __enter__(self) -> "MendWatch":
        return self

    def __exit__(self, exc_type: Optional[Type[BaseException]], exc_val: Optional[BaseException], exc_tb: Any) -> bool:
        if exc_val is None or not isinstance(exc_val, Exception):
            return False
        self.capture_exception(exc_val)
        return not self.raise_on_error

    def __call__(self, func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                self.capture_exception(exc)
                if self.raise_on_error:
                    raise
                return None

        return wrapper


def mend_watch(endpoint: Optional[str] = None, repo: Optional[str] = None, token: Optional[str] = None) -> MendWatch:
    """Decorator form: `@mend_watch(repo="org/repo")`."""
    return MendWatch(endpoint=endpoint, repo=repo, token=token)


def init_mend(endpoint: Optional[str] = None, repo: Optional[str] = None, token: Optional[str] = None) -> MendWatch:
    """Installs Mend as the process-wide handler for uncaught exceptions."""
    watcher = MendWatch(endpoint=endpoint, repo=repo, token=token)
    previous = sys.excepthook

    def hook(exc_type: Type[BaseException], exc_val: BaseException, exc_tb: Any) -> None:
        if isinstance(exc_val, Exception):
            watcher.capture_exception(exc_val)
        previous(exc_type, exc_val, exc_tb)

    sys.excepthook = hook
    return watcher
