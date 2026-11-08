"""
Mend — GitHub API Integration Client.
Provides multi-tenant remote repository operations: branch creation,
file commits, and automatic Pull Request generation.
"""
from __future__ import annotations

import base64
import json
import ssl
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

SSL_CTX = (
    ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    if Path("/etc/ssl/cert.pem").exists()
    else ssl.create_default_context()
)


class GitHubClient:
    def __init__(self, token: Optional[str] = None, default_repo: str = "Rampop01/Mend") -> None:
        self.token = token.strip() if token else None
        self.default_repo = default_repo.strip()

    def _headers(self, token: Optional[str] = None) -> Dict[str, str]:
        tok = token or self.token
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "Mend-Autonomous-Agent/2.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        return headers

    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        token: Optional[str] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        url = f"https://api.github.com/{endpoint.lstrip('/')}"
        payload = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(
            url,
            data=payload,
            headers=self._headers(token),
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=12, context=SSL_CTX) as resp:
                status = resp.status
                body = resp.read().decode("utf-8")
                return status, json.loads(body) if body else {}
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8")
            try:
                err_data = json.loads(body)
            except Exception:
                err_data = {"message": body or str(err)}
            return err.code, err_data
        except Exception as ex:
            return 500, {"message": str(ex)}

    def verify_repo(self, repo: str, token: Optional[str] = None) -> Dict[str, Any]:
        """Verifies that a repository exists and checks user permissions."""
        clean_repo = repo.strip().replace("https://github.com/", "").rstrip("/")
        status, data = self._request("GET", f"repos/{clean_repo}", token=token)
        if status == 200:
            return {
                "ok": True,
                "repo": clean_repo,
                "full_name": data.get("full_name", clean_repo),
                "default_branch": data.get("default_branch", "main"),
                "private": data.get("private", False),
                "permissions": data.get("permissions", {}),
            }
        return {"ok": False, "repo": clean_repo, "error": data.get("message", f"HTTP {status}")}

    def get_branch_head_sha(self, repo: str, branch: str = "main", token: Optional[str] = None) -> Optional[str]:
        clean_repo = repo.strip().replace("https://github.com/", "").rstrip("/")
        status, data = self._request("GET", f"repos/{clean_repo}/git/ref/heads/{branch}", token=token)
        if status == 200:
            return data.get("object", {}).get("sha")
        return None

    def create_branch(
        self,
        repo: str,
        new_branch: str,
        base_branch: str = "main",
        token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Creates a new branch on GitHub pointing to the base branch HEAD."""
        clean_repo = repo.strip().replace("https://github.com/", "").rstrip("/")
        base_sha = self.get_branch_head_sha(clean_repo, base_branch, token=token)
        if not base_sha:
            return {"ok": False, "error": f"Base branch '{base_branch}' not found in {clean_repo}"}

        status, data = self._request(
            "POST",
            f"repos/{clean_repo}/git/refs",
            data={"ref": f"refs/heads/{new_branch}", "sha": base_sha},
            token=token,
        )
        if status in (200, 201):
            return {"ok": True, "branch": new_branch, "sha": base_sha}
        return {"ok": False, "error": data.get("message", f"HTTP {status}")}

    def commit_file(
        self,
        repo: str,
        branch: str,
        file_path: str,
        content: str,
        commit_message: str,
        token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Creates or updates a file on a specific branch on GitHub."""
        clean_repo = repo.strip().replace("https://github.com/", "").rstrip("/")
        # Check if file exists to fetch sha
        status, file_data = self._request(
            "GET",
            f"repos/{clean_repo}/contents/{file_path}?ref={branch}",
            token=token,
        )
        existing_sha = file_data.get("sha") if status == 200 else None

        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        payload: Dict[str, Any] = {
            "message": commit_message,
            "content": encoded,
            "branch": branch,
        }
        if existing_sha:
            payload["sha"] = existing_sha

        c_status, c_data = self._request(
            "PUT",
            f"repos/{clean_repo}/contents/{file_path}",
            data=payload,
            token=token,
        )
        if c_status in (200, 201):
            commit_info = c_data.get("commit", {})
            return {
                "ok": True,
                "commit_sha": commit_info.get("sha", "")[:7],
                "commit_url": commit_info.get("html_url", ""),
            }
        return {"ok": False, "error": c_data.get("message", f"HTTP {c_status}")}

    def create_pull_request(
        self,
        repo: str,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str = "main",
        token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Opens a Pull Request on GitHub."""
        clean_repo = repo.strip().replace("https://github.com/", "").rstrip("/")
        status, data = self._request(
            "POST",
            f"repos/{clean_repo}/pulls",
            data={
                "title": title,
                "body": body,
                "head": head_branch,
                "base": base_branch,
            },
            token=token,
        )
        if status in (200, 201):
            return {
                "ok": True,
                "pr_number": data.get("number"),
                "pr_url": data.get("html_url"),
                "state": data.get("state"),
            }
        return {"ok": False, "error": data.get("message", f"HTTP {status}")}

