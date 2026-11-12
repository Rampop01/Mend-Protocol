"""
GitHub client: opens a pull request carrying an approved fix.

The pull request is only opened when the file on the base branch is identical
to the file the patch was verified against, so the PR diff is exactly the
reviewed patch and nothing else from the local checkout.
"""
from __future__ import annotations

import base64
import hashlib
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def _ssl_context() -> ssl.SSLContext:
    cafile = "/etc/ssl/cert.pem"
    return ssl.create_default_context(cafile=cafile) if Path(cafile).exists() else ssl.create_default_context()


class GitHubClient:
    def __init__(self, token: Optional[str] = None) -> None:
        self.token = token.strip() if token else ""

    def _request(self, method: str, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "mend-protocol",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(
            f"https://api.github.com/{endpoint.lstrip('/')}",
            data=json.dumps(data).encode("utf-8") if data is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=20, context=_ssl_context()) as resp:
                body = resp.read().decode("utf-8")
                return resp.status, json.loads(body) if body else {}
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", "replace")
            try:
                return err.code, json.loads(body)
            except ValueError:
                return err.code, {"message": body or str(err)}
        except Exception as exc:
            return 0, {"message": str(exc)}

    @staticmethod
    def _error(status: int, data: Any) -> str:
        message = data.get("message") if isinstance(data, dict) else None
        return f"GitHub {status}: {message or 'request failed'}"

    def open_fix_pull_request(
        self,
        repo: str,
        base_branch: Optional[str],
        branch: str,
        rel_path: str,
        original_sha256: str,
        patched_source: str,
        title: str,
        body: str,
    ) -> Dict[str, Any]:
        status, info = self._request("GET", f"repos/{repo}")
        if status != 200:
            return {"ok": False, "error": self._error(status, info)}
        base = base_branch or info.get("default_branch", "main")
        quoted_path = urllib.parse.quote(rel_path)

        status, remote = self._request("GET", f"repos/{repo}/contents/{quoted_path}?ref={urllib.parse.quote(base)}")
        if status != 200:
            return {"ok": False, "error": f"{rel_path} not found on {repo}@{base} ({self._error(status, remote)})"}
        try:
            remote_text = base64.b64decode(remote.get("content", "")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return {"ok": False, "error": f"Could not decode {rel_path} from {repo}@{base}"}
        if hashlib.sha256(remote_text.encode("utf-8")).hexdigest() != original_sha256:
            return {
                "ok": False,
                "error": f"{rel_path} on {repo}@{base} differs from the file the patch was verified against",
            }

        status, ref = self._request("GET", f"repos/{repo}/git/ref/heads/{urllib.parse.quote(base)}")
        if status != 200:
            return {"ok": False, "error": self._error(status, ref)}
        status, created = self._request(
            "POST", f"repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": ref["object"]["sha"]}
        )
        if status not in (200, 201):
            return {"ok": False, "error": self._error(status, created)}

        status, commit = self._request(
            "PUT",
            f"repos/{repo}/contents/{quoted_path}",
            {
                "message": title,
                "content": base64.b64encode(patched_source.encode("utf-8")).decode("ascii"),
                "branch": branch,
                "sha": remote["sha"],
            },
        )
        if status not in (200, 201):
            return {"ok": False, "error": self._error(status, commit)}

        status, pr = self._request(
            "POST", f"repos/{repo}/pulls", {"title": title, "body": body, "head": branch, "base": base}
        )
        if status not in (200, 201):
            return {"ok": False, "error": self._error(status, pr)}
        return {"ok": True, "pr_url": pr.get("html_url"), "pr_number": pr.get("number"), "branch": branch}

    def get_repo(self, repo: str) -> Dict[str, Any]:
        status, info = self._request("GET", f"repos/{repo}")
        if status != 200:
            return {"ok": False, "error": self._error(status, info)}
        return {
            "ok": True,
            "full_name": info.get("full_name", repo),
            "default_branch": info.get("default_branch", "main"),
            "private": bool(info.get("private")),
            "clone_url": info.get("clone_url") or f"https://github.com/{repo}.git",
            "can_push": bool((info.get("permissions") or {}).get("push")),
        }

    def comment(self, repo: str, number: int, body: str) -> Dict[str, Any]:
        status, data = self._request("POST", f"repos/{repo}/issues/{number}/comments", {"body": body})
        if status not in (200, 201):
            return {"ok": False, "error": self._error(status, data)}
        return {"ok": True, "url": data.get("html_url")}
