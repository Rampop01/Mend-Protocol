"""
Guard agent: independent review of a forge agent's candidate patch.

The guard works in two phases:

  screen()  static policy gate on what the patch *introduces*, run before any
            candidate code is executed. A patch that fails here is never run.
  verify()  re-runs the project's test suite itself in a fresh sandbox, so it
            does not rely on the forge agent's claimed result, then scores the
            patch.

The score is computed from the checks below. It is not a constant, and every
component is listed in the report so a reviewer can see where it came from.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from mend.sandbox import TestRun
from mend.swarm.forge import Candidate
from mend.swarm.recon import ReconReport

FORBIDDEN_NAMES = {"eval", "exec", "compile", "__import__", "breakpoint"}
FORBIDDEN_CALL_PREFIXES = (
    "os.system", "os.popen", "os.exec", "os.spawn", "os.remove", "os.unlink", "os.rmdir", "os.kill",
    "subprocess.", "socket.", "pty.", "ctypes.", "shutil.rmtree", "pickle.load", "marshal.load",
    "urllib.request.", "requests.", "http.client.",
)
FORBIDDEN_IMPORTS = {
    "subprocess", "socket", "pty", "ctypes", "pickle", "marshal", "requests", "urllib", "http", "ftplib", "smtplib",
}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),  # Telegram bot token
    re.compile(r"ed25519:[1-9A-HJ-NP-Za-km-z]{80,}"),  # NEAR secret key
    re.compile(r"BEGIN (?:RSA |OPENSSH |EC |)PRIVATE KEY"),
]

MAX_CHANGED_LINES = 60

# Score weights. They sum to 100.
W_TESTS_PASS = 40
W_FIXES_FAILING_TEST = 20
W_BLAST_RADIUS = 15
W_SCOPE = 10
W_STATIC_CLEAN = 10
W_INDEPENDENT_RERUN = 5


@dataclass
class Check:
    name: str
    status: str  # PASS, FAIL, WARN
    detail: str
    points: int = 0
    max_points: int = 0


@dataclass
class GuardReport:
    passed: bool
    score: int
    checks: List[Check] = field(default_factory=list)
    lines_added: int = 0
    lines_removed: int = 0
    summary: str = ""
    executed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def report_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode("utf-8")).hexdigest()


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _swallows(handler: ast.ExceptHandler) -> bool:
    """A handler that catches everything and then does nothing with it."""
    broad = handler.type is None or _call_name(handler.type) in ("Exception", "BaseException")
    if not broad:
        return False
    for stmt in handler.body:
        if isinstance(stmt, (ast.Pass, ast.Continue)):
            continue
        if isinstance(stmt, ast.Return) and (stmt.value is None or isinstance(stmt.value, ast.Constant)):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        return False
    return True


def _risk_profile(tree: ast.AST) -> Counter:
    """Counts risky constructs so a patch is judged on what it adds, not on what was already there."""
    found: Counter = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in FORBIDDEN_NAMES or any(name.startswith(p) for p in FORBIDDEN_CALL_PREFIXES):
                found[f"call:{name}"] += 1
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_IMPORTS:
                    found[f"import:{alias.name}"] += 1
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in FORBIDDEN_IMPORTS:
                found[f"import:{node.module}"] += 1
        elif isinstance(node, ast.ExceptHandler) and _swallows(node):
            found["swallowed-exception"] += 1
    return found


def _changed_lines(original: str, patched: str) -> Tuple[List[str], int, List[int]]:
    """Returns (added lines, removed count, original line numbers touched)."""
    a, b = original.splitlines(), patched.splitlines()
    added: List[str] = []
    removed = 0
    touched: List[int] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        added.extend(b[j1:j2])
        removed += i2 - i1
        # An insertion touches the line it is inserted before.
        touched.extend(range(i1 + 1, i2 + 1) if i2 > i1 else [min(i1 + 1, len(a))])
    return added, removed, touched


class GuardAgent:
    role = "guard"

    def screen(self, recon: ReconReport, candidate: Candidate) -> GuardReport:
        """Static gate. Runs before the candidate is executed anywhere."""
        checks: List[Check] = []
        added, removed, touched = _changed_lines(candidate.original_source, candidate.patched_source)

        try:
            patched_tree = ast.parse(candidate.patched_source)
            checks.append(Check("Patched file parses", "PASS", "Valid Python syntax"))
        except SyntaxError as exc:
            checks.append(Check("Patched file parses", "FAIL", f"SyntaxError at line {exc.lineno}: {exc.msg}"))
            return self._finish(checks, len(added), removed, executed=False)

        try:
            baseline_risk = _risk_profile(ast.parse(candidate.original_source))
        except SyntaxError:
            baseline_risk = Counter()
        introduced = _risk_profile(patched_tree) - baseline_risk
        dangerous = sorted(k for k in introduced if k != "swallowed-exception")
        checks.append(
            Check(
                "No dangerous calls or imports introduced",
                "FAIL" if dangerous else "PASS",
                f"Patch introduces: {', '.join(dangerous)}" if dangerous else "Nothing on the deny list was added",
            )
        )
        swallowed = introduced.get("swallowed-exception", 0)
        checks.append(
            Check(
                "Does not swallow exceptions",
                "FAIL" if swallowed else "PASS",
                f"Patch adds {swallowed} catch-all handler(s) that discard the error"
                if swallowed else "No catch-all handler that hides the error",
            )
        )

        leaked = any(p.search(line) for line in added for p in SECRET_PATTERNS)
        checks.append(
            Check(
                "No credentials in added lines",
                "FAIL" if leaked else "PASS",
                "A credential-shaped string appears in the patch" if leaked else "No credential patterns in added lines",
            )
        )

        is_test_file = any(part.startswith("test") for part in candidate.rel_path.replace("\\", "/").split("/"))
        checks.append(
            Check(
                "Does not edit tests",
                "FAIL" if is_test_file else "PASS",
                "Patch targets a test file" if is_test_file else "Patch targets application code",
            )
        )

        changed = len(added) + removed
        checks.append(
            Check(
                "Blast radius within limit",
                "FAIL" if changed > MAX_CHANGED_LINES else "PASS",
                f"+{len(added)} / -{removed} lines (limit {MAX_CHANGED_LINES})",
            )
        )
        report = self._finish(checks, len(added), removed, executed=False)
        report.checks.append(self._scope_check(recon, touched))
        return report

    def verify(
        self,
        recon: ReconReport,
        candidate: Candidate,
        screen: GuardReport,
        forge_run: TestRun,
        guard_run: TestRun,
    ) -> GuardReport:
        """Scores a screened patch using the guard's own test run."""
        checks = [c for c in screen.checks if c.name != "Confined to the crashing scope"]
        _, _, touched = _changed_lines(candidate.original_source, candidate.patched_source)
        static_ok = all(c.status != "FAIL" for c in checks)
        for check in checks:
            check.points = check.max_points = 0
        checks.append(
            Check(
                "Static policy gate", "PASS" if static_ok else "FAIL",
                "All static checks passed" if static_ok else "One or more static checks failed",
                W_STATIC_CLEAN if static_ok else 0, W_STATIC_CLEAN,
            )
        )

        tests_ok = guard_run.ok
        if guard_run.error:
            detail = guard_run.error
        else:
            detail = f"{guard_run.tests_run - guard_run.tests_failed}/{guard_run.tests_run} tests pass with the patch applied"
        checks.append(Check("Test suite passes", "PASS" if tests_ok else "FAIL", detail,
                            W_TESTS_PASS if tests_ok else 0, W_TESTS_PASS))

        baseline = recon.baseline or {}
        was_failing = set(baseline.get("failed_tests") or [])
        still_failing = set(guard_run.failed_tests)
        fixed = sorted(was_failing - still_failing)
        if baseline.get("tests_failed") and tests_ok:
            names = ", ".join(n.rsplit(".", 1)[-1] for n in fixed[:3]) or f"{baseline['tests_failed']} test(s)"
            checks.append(Check("Fixes a previously failing test", "PASS",
                                f"Failing before the patch, passing after: {names}",
                                W_FIXES_FAILING_TEST, W_FIXES_FAILING_TEST))
        else:
            reason = (
                "No test failed before the patch, so the suite does not demonstrate the fix"
                if not baseline.get("tests_failed") else "Previously failing tests still fail"
            )
            checks.append(Check("Fixes a previously failing test", "WARN", reason, 0, W_FIXES_FAILING_TEST))

        agrees = (forge_run.tests_run, forge_run.tests_failed) == (guard_run.tests_run, guard_run.tests_failed)
        checks.append(
            Check(
                "Independent re-run matches the forge agent's claim",
                "PASS" if agrees else "FAIL",
                f"Forge reported {forge_run.tests_failed} failed of {forge_run.tests_run}; "
                f"guard observed {guard_run.tests_failed} failed of {guard_run.tests_run}",
                W_INDEPENDENT_RERUN if agrees else 0, W_INDEPENDENT_RERUN,
            )
        )

        changed = screen.lines_added + screen.lines_removed
        # Full marks up to 10 changed lines, falling linearly to zero at the limit.
        radius = W_BLAST_RADIUS if changed <= 10 else max(
            0, round(W_BLAST_RADIUS * (MAX_CHANGED_LINES - changed) / (MAX_CHANGED_LINES - 10))
        )
        checks.append(Check("Small change", "PASS" if radius else "WARN",
                            f"{changed} changed line(s)", radius, W_BLAST_RADIUS))

        scope = self._scope_check(recon, touched)
        scope.max_points = W_SCOPE
        scope.points = W_SCOPE if scope.status == "PASS" else 0
        checks.append(scope)

        score = sum(c.points for c in checks)
        passed = static_ok and tests_ok and agrees
        if not static_ok:
            score = min(score, 20)
        report = GuardReport(
            passed=passed,
            score=score,
            checks=checks,
            lines_added=screen.lines_added,
            lines_removed=screen.lines_removed,
            executed=True,
        )
        report.summary = (
            f"{'Approved' if passed else 'Rejected'} with score {score}/100: "
            + "; ".join(f"{c.name}: {c.status}" for c in checks if c.max_points)
        )
        return report

    def _scope_check(self, recon: ReconReport, touched: List[int]) -> Check:
        inside = bool(touched) and all(recon.scope_start <= n <= recon.scope_end for n in touched)
        return Check(
            "Confined to the crashing scope",
            "PASS" if inside else "WARN",
            f"All edits fall inside {recon.enclosing_scope} (lines {recon.scope_start}-{recon.scope_end})"
            if inside else f"Patch edits lines outside {recon.enclosing_scope}",
        )

    def _finish(self, checks: List[Check], added: int, removed: int, executed: bool) -> GuardReport:
        failed = [c for c in checks if c.status == "FAIL"]
        report = GuardReport(
            passed=not failed, score=0, checks=checks, lines_added=added, lines_removed=removed, executed=executed
        )
        report.summary = (
            "Blocked before execution: " + "; ".join(f"{c.name} ({c.detail})" for c in failed)
            if failed else "Static policy gate passed"
        )
        return report
