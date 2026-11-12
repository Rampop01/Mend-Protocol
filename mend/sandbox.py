"""
Runs a project's test suite against a candidate patch in a throwaway copy.

The live checkout is never modified during verification. Each run copies the
project into a fresh temporary directory, overlays the candidate file, runs the
project's configured test command with secrets stripped from the environment,
and deletes the copy.

This is isolation of the working tree, not a security boundary: the tests run
as the same OS user. The guard agent's static gate runs before any candidate
code is executed, and Mend should itself run in a container when it handles
code you do not trust.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from mend.config import Project

# Never copied into a sandbox: VCS data, dependencies, build output and secrets.
ALWAYS_IGNORED = (".git", ".venv", "venv", "node_modules", "__pycache__", ".mend", "*.pyc", ".env", ".env.*")

# Variables that never reach a test subprocess.
SECRET_ENV_MARKERS = ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL")

MAX_OUTPUT_CHARS = 6000


@dataclass
class TestRun:
    ok: bool
    tests_run: int
    tests_failed: int
    failed_tests: List[str]
    returncode: Optional[int]
    duration_ms: int
    output: str
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _clean_env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not any(m in k.upper() for m in SECRET_ENV_MARKERS)}
    # The copy under test must not report its own crashes back to Mend.
    env["MEND_DISABLED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def parse_test_output(output: str, returncode: int) -> Dict[str, Any]:
    """Extracts counts from unittest or pytest output."""
    failed_names = re.findall(r"^(?:FAIL|ERROR): (\S+) \(([^)]+)\)", output, flags=re.MULTILINE)
    # Python < 3.11 prints "test_x (module.Class)"; newer versions print "test_x (module.Class.test_x)".
    names = [cls if cls.endswith("." + name) else f"{cls}.{name}" for name, cls in failed_names]

    ran = re.search(r"^Ran (\d+) tests?", output, flags=re.MULTILINE)
    if ran:  # unittest
        total = int(ran.group(1))
        failures = sum(int(n) for n in re.findall(r"(?:failures|errors)=(\d+)", output))
        return {"tests_run": total, "tests_failed": min(failures, total), "failed_tests": names}

    summary = re.search(r"=+ (.*?) in [\d.]+s", output)
    if summary:  # pytest
        counts = {kind: int(n) for n, kind in re.findall(r"(\d+) (passed|failed|error|errors)", summary.group(1))}
        failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
        total = counts.get("passed", 0) + failed
        names = re.findall(r"^(?:FAILED|ERROR) (\S+)", output, flags=re.MULTILINE) or names
        return {"tests_run": total, "tests_failed": failed, "failed_tests": names}

    # Unknown runner: we only know whether the command succeeded.
    return {"tests_run": 0, "tests_failed": 0 if returncode == 0 else 1, "failed_tests": names}


def run_tests(project: Project, overlay: Optional[Dict[str, str]] = None) -> TestRun:
    """
    Runs the project's test command in a fresh copy of the project.
    `overlay` maps project-relative paths to replacement file contents.
    """
    started = time.time()
    workdir = Path(tempfile.mkdtemp(prefix="mend-sandbox-"))
    copy_root = workdir / "project"
    try:
        ignore = shutil.ignore_patterns(*ALWAYS_IGNORED, *project.sandbox_ignore)
        shutil.copytree(project.root, copy_root, ignore=ignore, symlinks=True)
        for rel_path, content in (overlay or {}).items():
            target = (copy_root / rel_path).resolve()
            target.relative_to(copy_root.resolve())  # refuses paths that escape the copy
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        try:
            proc = subprocess.run(
                project.test_command,
                cwd=str(copy_root),
                env=_clean_env(),
                capture_output=True,
                text=True,
                timeout=project.test_timeout,
            )
        except subprocess.TimeoutExpired:
            return TestRun(
                ok=False, tests_run=0, tests_failed=0, failed_tests=[], returncode=None,
                duration_ms=int((time.time() - started) * 1000), output="",
                error=f"Test command timed out after {project.test_timeout}s",
            )
        except OSError as exc:
            return TestRun(
                ok=False, tests_run=0, tests_failed=0, failed_tests=[], returncode=None,
                duration_ms=int((time.time() - started) * 1000), output="",
                error=f"Could not start test command: {exc}",
            )

        output = (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).replace(str(copy_root), "<project>")
        counts = parse_test_output(output, proc.returncode)
        ok = proc.returncode == 0 and counts["tests_failed"] == 0 and counts["tests_run"] > 0
        error = ""
        if proc.returncode == 0 and counts["tests_run"] == 0:
            error = "Test command succeeded but reported no tests, so it proves nothing"
        return TestRun(
            ok=ok,
            tests_run=counts["tests_run"],
            tests_failed=counts["tests_failed"],
            failed_tests=counts["failed_tests"],
            returncode=proc.returncode,
            duration_ms=int((time.time() - started) * 1000),
            output=output.strip()[-MAX_OUTPUT_CHARS:],
            error=error,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
