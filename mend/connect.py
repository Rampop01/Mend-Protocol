"""
`mend connect owner/repo`: puts a GitHub repository under Mend's care.

Mend clones the repository into its own workspace (.mend/workspaces), detects
how to run its tests, checks that the suite runs, and opens a bounty treasury
for it on NEAR. From then on Mend keeps the workspace in sync with GitHub and
delivers every approved fix as a pull request; it never pushes to the base
branch.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from mend.config import Project, Settings
from mend.integrations.github import GitHubClient
from mend.sandbox import run_tests

REPO_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")


class ConnectError(Exception):
    pass


def normalize_repo(value: str) -> str:
    repo = value.strip()
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:", "github.com/"):
        if repo.startswith(prefix):
            repo = repo[len(prefix):]
    repo = repo.rstrip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not REPO_PATTERN.match(repo):
        raise ConnectError(f"Not a GitHub repository name: {value!r}. Use owner/repo.")
    return repo


def _git(args: List[str], cwd: Optional[Path] = None, token: str = "", timeout: int = 180) -> str:
    """Runs git. The token is passed per command, so it is never written to .git/config."""
    command = ["git"]
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        command += ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]
    command += args
    try:
        proc = subprocess.run(command, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ConnectError(f"git {args[0]} timed out")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().replace(token, "***") if token else (proc.stderr or proc.stdout).strip()
        raise ConnectError(f"git {args[0]} failed: {detail[-400:]}")
    return proc.stdout


def sync_workspace(project: Project, token: str = "") -> str:
    """Resets a managed clone to the latest commit of its base branch. Returns that commit."""
    branch = project.base_branch or "main"
    _git(["fetch", "--quiet", "origin", branch], cwd=project.root, token=token)
    _git(["reset", "--quiet", "--hard", f"origin/{branch}"], cwd=project.root)
    _git(["clean", "-fdq"], cwd=project.root)
    return _git(["rev-parse", "HEAD"], cwd=project.root).strip()


def detect_test_command(root: Path) -> List[str]:
    """Picks pytest when the project is configured for it, otherwise unittest discovery."""
    python = sys.executable
    pyproject = root / "pyproject.toml"
    uses_pytest = (
        (root / "pytest.ini").exists()
        or (root / "conftest.py").exists()
        or (pyproject.exists() and "[tool.pytest" in pyproject.read_text(encoding="utf-8", errors="ignore"))
        or any(
            "pytest" in (root / name).read_text(encoding="utf-8", errors="ignore")
            for name in ("requirements-dev.txt", "requirements.txt", "setup.cfg", "tox.ini")
            if (root / name).exists()
        )
    )
    if uses_pytest:
        return [python, "-m", "pytest", "-q"]
    for folder in ("tests", "test"):
        if (root / folder).is_dir():
            # `-t .` only works when the folder is a package; otherwise discover it as a top-level dir.
            top = ["-t", "."] if (root / folder / "__init__.py").exists() else []
            return [python, "-m", "unittest", "discover", "-s", folder, *top]
    if list(root.glob("test_*.py")) or list(root.glob("*_test.py")):
        return [python, "-m", "unittest", "discover", "-s", "."]
    raise ConnectError("Could not find a test suite. Pass --test-command to say how to run the tests.")


def _save_connected(settings: Settings, project: Project) -> None:
    path = settings.connected_projects_file
    data: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"projects": []}
    entries = [e for e in data.get("projects", []) if e.get("repo", "").lower() != project.repo.lower()]
    entries.append({
        "repo": project.repo,
        "root": str(project.root),
        "test_command": project.test_command,
        "test_timeout": project.test_timeout,
        "base_branch": project.base_branch,
        "owner_telegram_id": project.owner_telegram_id,
        "ingest_token_hash": project.ingest_token_hash,
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"projects": entries}, indent=2) + "\n", encoding="utf-8")


def connect_repo(
    settings: Settings,
    repo_name: str,
    test_command: Optional[List[str]] = None,
    base_branch: Optional[str] = None,
    register_on_chain: bool = True,
    github: Optional[GitHubClient] = None,
    clone_url: Optional[str] = None,
    log: Callable[[str], None] = print,
    owner_telegram_id: str = "",
) -> Dict[str, Any]:
    repo = normalize_repo(repo_name)
    github = github or GitHubClient(settings.github_token)
    if not github.token:
        raise ConnectError("No GitHub token. Run `gh auth login`, or set GITHUB_TOKEN in .env.")

    info = github.get_repo(repo)
    if not info.get("ok"):
        raise ConnectError(f"Cannot open {repo}: {info.get('error')}")
    if not info.get("can_push"):
        raise ConnectError(f"The GitHub token cannot push to {repo}, so Mend could not open pull requests there.")
    branch = base_branch or info["default_branch"]
    root = (settings.workspaces_dir / repo.replace("/", "__")).resolve()

    existing = settings.project_by_repo(info["full_name"])
    if existing is not None and existing.owner_telegram_id and existing.owner_telegram_id != owner_telegram_id:
        raise ConnectError(f"{info['full_name']} is already connected to Mend by another user.")
    project = Project(repo=info["full_name"], root=root, test_command=[], base_branch=branch, managed=True,
                      owner_telegram_id=owner_telegram_id)
    from mend.accounts import new_ingest_token
    ingest_token = new_ingest_token()
    project.ingest_token_hash = hashlib.sha256(ingest_token.encode()).hexdigest()
    if (root / ".git").is_dir():
        log(f"Updating the existing workspace {root}")
    else:
        log(f"Cloning {repo} into {root}")
        root.parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", "--quiet", "--branch", branch, clone_url or info["clone_url"], str(root)],
             token=github.token, timeout=600)
    commit = sync_workspace(project, token=github.token)

    project.test_command = test_command or detect_test_command(root)
    log(f"Running the test suite once: {' '.join(project.test_command)}")
    baseline = run_tests(project)
    if baseline.tests_run == 0:
        raise ConnectError(
            f"The test command did not run any tests ({baseline.error or 'no test output recognised'}). "
            "Pass --test-command to set it."
        )
    log(f"{baseline.tests_run - baseline.tests_failed}/{baseline.tests_run} tests pass on {branch} at {commit[:7]}")
    _save_connected(settings, project)
    # Make the project live for a running server without a restart.
    settings.projects = [p for p in settings.projects if p.repo.lower() != project.repo.lower()] + [project]

    treasury: Optional[Dict[str, Any]] = None
    if register_on_chain:
        if not settings.near_keys_file.exists():
            log("NEAR is not set up yet, so no treasury was opened. Run `mend setup`, then `mend connect` again.")
        else:
            from mend.integrations.escrow import register_repo_treasury
            treasury = register_repo_treasury(settings.near_keys_file, project.repo, log=log,
                                              deposit_near=settings.treasury_deposit_near,
                                              max_bounty_near=settings.max_bounty_near)

    return {
        "repo": project.repo,
        "root": str(root),
        "base_branch": branch,
        "commit": commit,
        "test_command": project.test_command,
        "tests_run": baseline.tests_run,
        "tests_failed": baseline.tests_failed,
        "treasury": treasury,
        "ingest_token": ingest_token,
        "project": project,
    }
