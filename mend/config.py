"""
Server configuration.

Everything security-relevant lives here, on the server, and never in the
incident payload: which directories Mend may read and patch, which command
proves a fix, how large a bounty may be, and who may approve.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


def load_dotenv(path: Path) -> None:
    """Loads KEY=VALUE lines into the environment without overriding it."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


@dataclass
class Project:
    """A codebase Mend is allowed to repair."""

    repo: str
    root: Path
    test_command: List[str]
    test_timeout: int = 120
    base_branch: Optional[str] = None
    sandbox_ignore: List[str] = field(default_factory=list)
    # Managed projects are clones Mend owns (created by `mend connect`). Mend keeps them
    # in sync with GitHub and delivers fixes only as pull requests.
    managed: bool = False

    def contains(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root)
            return True
        except ValueError:
            return False

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()


@dataclass
class Settings:
    base_dir: Path
    host: str = "127.0.0.1"
    port: int = 8770
    ingest_token: str = ""
    db_path: Path = Path("data/mend.db")
    projects: List[Project] = field(default_factory=list)

    telegram_token: str = ""
    telegram_chat_id: str = ""
    telegram_approver_ids: List[str] = field(default_factory=list)

    github_token: str = ""
    github_token_source: str = ""

    llm_api_key: str = ""
    llm_base_url: str = "https://cloud-api.near.ai/v1"
    llm_model: str = "anthropic/claude-sonnet-5-5"

    near_network: str = "testnet"
    near_keys_file: Path = Path(".mend/testnet.json")
    bounty_near: str = "0.25"
    connected_projects_file: Path = Path(".mend/projects.json")
    workspaces_dir: Path = Path(".mend/workspaces")

    def project_for(self, path: Path) -> Optional[Project]:
        for project in self.projects:
            if project.contains(path):
                return project
        return None

    def project_by_repo(self, repo: str) -> Optional[Project]:
        for project in self.projects:
            if project.repo.lower() == repo.lower():
                return project
        return None

    @property
    def is_loopback(self) -> bool:
        return self.host in ("127.0.0.1", "localhost", "::1")


def _parse_projects(path: Path, managed: bool) -> List[Project]:
    data: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    projects: List[Project] = []
    for entry in data.get("projects", []):
        command = entry.get("test_command")
        if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
            raise SystemExit(f"{path}: test_command for {entry.get('repo')} must be a non-empty list of strings")
        root = Path(entry["root"])
        if not root.is_absolute():
            root = path.parent / root
        root = root.resolve()
        if not root.is_dir():
            raise SystemExit(f"{path}: project root does not exist: {root}")
        projects.append(
            Project(
                repo=entry["repo"],
                root=root,
                test_command=command,
                test_timeout=int(entry.get("test_timeout", 120)),
                base_branch=entry.get("base_branch"),
                sandbox_ignore=[str(x) for x in entry.get("sandbox_ignore", [])],
                managed=managed,
            )
        )
    return projects


def load_projects(settings: "Settings", projects_file: Path) -> List[Project]:
    """Projects listed by hand in mend.projects.json plus those added with `mend connect`."""
    projects: List[Project] = []
    if projects_file.exists():
        projects += _parse_projects(projects_file, managed=False)
    if settings.connected_projects_file.exists():
        known = {p.repo.lower() for p in projects}
        projects += [p for p in _parse_projects(settings.connected_projects_file, managed=True)
                     if p.repo.lower() not in known]
    if not projects:
        raise SystemExit(
            "No projects configured. Run `mend connect owner/repo`, or list a local project in mend.projects.json."
        )
    return projects


def _gh_cli_token() -> str:
    """Reuses the GitHub CLI login if there is one, so `gh auth login` is enough."""
    if not shutil.which("gh"):
        return ""
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def load_settings(base_dir: Optional[Path] = None, require_projects: bool = True) -> Settings:
    base = (base_dir or Path.cwd()).resolve()
    load_dotenv(base / ".env")
    env = os.environ

    def rel(value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else base / p

    settings = Settings(
        base_dir=base,
        host=env.get("MEND_HOST", "127.0.0.1"),
        port=int(env.get("PORT", env.get("MEND_PORT", "8770"))),
        ingest_token=env.get("MEND_INGEST_TOKEN", ""),
        db_path=rel(env.get("MEND_DB_PATH", "data/mend.db")),
        telegram_token=env.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=env.get("TELEGRAM_CHAT_ID", ""),
        telegram_approver_ids=[s.strip() for s in env.get("TELEGRAM_APPROVER_IDS", "").split(",") if s.strip()],
        github_token=env.get("GITHUB_TOKEN", ""),
        llm_api_key=env.get("NEAR_AI_API_KEY", env.get("MEND_LLM_API_KEY", "")),
        llm_base_url=env.get("MEND_LLM_BASE_URL", "https://cloud-api.near.ai/v1").rstrip("/"),
        llm_model=env.get("MEND_LLM_MODEL", "anthropic/claude-sonnet-5-5"),
        near_network=env.get("NEAR_NETWORK", "testnet"),
        near_keys_file=rel(env.get("MEND_KEYS_FILE", ".mend/testnet.json")),
        bounty_near=env.get("MEND_BOUNTY_NEAR", "0.25"),
    )
    settings.connected_projects_file = base / ".mend" / "projects.json"
    settings.workspaces_dir = base / ".mend" / "workspaces"
    if settings.github_token:
        settings.github_token_source = "GITHUB_TOKEN"
    elif env.get("MEND_USE_GH_CLI", "1") != "0":
        settings.github_token = _gh_cli_token()
        settings.github_token_source = "gh CLI login" if settings.github_token else ""
    if require_projects:
        settings.projects = load_projects(settings, rel(env.get("MEND_PROJECTS_FILE", "mend.projects.json")))
    return settings
