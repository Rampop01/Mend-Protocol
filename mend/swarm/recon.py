"""
Recon agent: locates the crash in the source tree and reproduces it.

Given the traceback frames reported by the SDK, recon picks the deepest frame
that belongs to a registered project, parses that file's AST to find the
failing statement and its enclosing function, and runs the project's test suite
unpatched to record which tests already fail. That baseline is what later lets
the guard tell a real fix from a patch that merely leaves the suite green.
"""
from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mend.config import Project, Settings
from mend.sandbox import TestRun, run_tests


@dataclass
class ReconReport:
    ok: bool
    summary: str
    repo: str = ""
    rel_path: str = ""
    line_number: int = 0
    error_type: str = ""
    error_message: str = ""
    failing_statement: str = ""
    enclosing_scope: str = "module"
    node_type: str = ""
    scope_start: int = 0
    scope_end: int = 0
    code_context: str = ""
    source_sha256: str = ""
    fingerprint: str = ""
    baseline: Optional[Dict[str, Any]] = None
    frame_locals: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def report_hash(self) -> str:
        """Digest committed on-chain by the recon agent."""
        payload = {k: v for k, v in self.to_dict().items() if k != "frame_locals"}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def crash_fingerprint(repo: str, rel_path: str, line_number: int, error_type: str) -> str:
    raw = f"{repo.lower()}|{rel_path}|{line_number}|{error_type}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _match_in_project(project: Project, reported: str) -> Optional[Path]:
    """
    Maps a path reported by the watched process to a file in the project.
    Accepts an absolute path inside the root, or a path from another checkout
    whose trailing components identify exactly one file under the root.
    """
    path = Path(reported)
    if path.is_absolute():
        resolved = path.resolve()
        if project.contains(resolved) and resolved.is_file():
            return resolved
    parts = [p for p in path.parts if p not in ("/", "\\")]
    for start in range(len(parts)):
        if len(parts) - start < 2 and len(parts) > 1:
            break  # a bare filename is too ambiguous to trust
        candidate = (project.root.joinpath(*parts[start:])).resolve()
        if project.contains(candidate) and candidate.is_file():
            return candidate
    return None


def locate_frame(
    settings: Settings, frames: List[Dict[str, Any]], repo_hint: Optional[str] = None
) -> Optional[Tuple[Project, Path, int, Dict[str, Any]]]:
    """Returns the deepest traceback frame that lies inside a registered project."""
    projects = list(settings.projects)
    if repo_hint:
        projects.sort(key=lambda p: p.repo.lower() != repo_hint.lower())
    for frame in reversed(frames):
        reported = str(frame.get("file") or "")
        try:
            line = int(frame.get("line") or 0)
        except (TypeError, ValueError):
            continue
        if not reported or line <= 0:
            continue
        for project in projects:
            match = _match_in_project(project, reported)
            if match is not None and match.suffix == ".py":
                return project, match, line, frame
    return None


class _Locator(ast.NodeVisitor):
    """Finds the innermost statement and the scope chain covering a line."""

    def __init__(self, target_line: int) -> None:
        self.target = target_line
        self.scopes: List[ast.AST] = []
        self.best_scopes: List[ast.AST] = []
        self.statement: Optional[ast.stmt] = None

    def _covers(self, node: ast.AST) -> bool:
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", start)
        return start is not None and start <= self.target <= (end or start)

    def generic_visit(self, node: ast.AST) -> None:
        is_scope = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        if is_scope:
            self.scopes.append(node)
        if isinstance(node, ast.stmt) and self._covers(node) and not is_scope:
            # Later (deeper) matches overwrite earlier ones, leaving the innermost statement.
            self.statement = node
            self.best_scopes = list(self.scopes)
        super().generic_visit(node)
        if is_scope:
            self.scopes.pop()


def _scope_label(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        return f"class {node.name}"
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {getattr(node, 'name', '?')}()"


class ReconAgent:
    role = "recon"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def analyze(
        self,
        project: Project,
        file_path: Path,
        line_number: int,
        error_type: str,
        error_message: str,
        frame_locals: Optional[Dict[str, str]] = None,
        run_baseline: bool = True,
    ) -> ReconReport:
        rel_path = project.relative(file_path)
        source = file_path.read_text(encoding="utf-8")
        lines = source.splitlines()
        base = dict(
            repo=project.repo,
            rel_path=rel_path,
            line_number=line_number,
            error_type=error_type,
            error_message=error_message,
            source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            fingerprint=crash_fingerprint(project.repo, rel_path, line_number, error_type),
            frame_locals=dict(frame_locals or {}),
        )
        if not 1 <= line_number <= len(lines):
            return ReconReport(ok=False, summary=f"Line {line_number} is outside {rel_path}", **base)

        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError as exc:
            return ReconReport(ok=False, summary=f"{rel_path} does not parse: {exc.msg} (line {exc.lineno})", **base)

        locator = _Locator(line_number)
        locator.visit(tree)
        statement = locator.statement
        if statement is None:
            return ReconReport(ok=False, summary=f"No statement found at {rel_path}:{line_number}", **base)

        functions = [s for s in locator.best_scopes if not isinstance(s, ast.ClassDef)]
        if functions:
            scope_node = functions[-1]
            decorators = getattr(scope_node, "decorator_list", [])
            scope_start = min([scope_node.lineno] + [d.lineno for d in decorators])
            scope_end = scope_node.end_lineno or scope_node.lineno
        else:
            scope_start, scope_end = statement.lineno, statement.end_lineno or statement.lineno
        scope_label = " -> ".join(_scope_label(s) for s in locator.best_scopes) or "module"

        ctx_start = max(1, min(scope_start, line_number - 8))
        ctx_end = min(len(lines), max(scope_end, line_number + 8))
        if ctx_end - ctx_start > 120:  # keep very long functions readable
            ctx_start, ctx_end = max(1, line_number - 60), min(len(lines), line_number + 60)
        context = "\n".join(
            f"{'>>' if n == line_number else '  '} {n:4d} | {lines[n - 1]}" for n in range(ctx_start, ctx_end + 1)
        )

        baseline: Optional[TestRun] = run_tests(project) if run_baseline else None
        failing_statement = lines[line_number - 1].strip()
        summary = f"{error_type} at {rel_path}:{line_number} in {scope_label} ({type(statement).__name__})."
        if baseline is not None:
            if baseline.error:
                summary += f" Baseline tests could not run: {baseline.error}."
            elif baseline.tests_failed:
                summary += f" Baseline: {baseline.tests_failed} of {baseline.tests_run} tests fail before any patch."
            else:
                summary += f" Baseline: all {baseline.tests_run} tests pass, so no test reproduces this crash."

        return ReconReport(
            ok=True,
            summary=summary,
            failing_statement=failing_statement,
            enclosing_scope=scope_label,
            node_type=type(statement).__name__,
            scope_start=scope_start,
            scope_end=scope_end,
            code_context=context,
            baseline=baseline.to_dict() if baseline else None,
            **base,
        )
