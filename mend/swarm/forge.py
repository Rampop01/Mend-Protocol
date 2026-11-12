"""
Forge agents: propose a candidate patch for a located crash.

Two independent strategies compete on every incident, each with its own NEAR
account:

  RuleForge  deterministic AST rewrites for a small set of crash classes.
             It declines when no rule applies; it never guesses.
  LLMForge   asks a language model for exact search/replace edits, given the
             crash, the surrounding code and the failing baseline tests.

A forge agent only proposes. Whether a proposal is safe and correct is decided
by the guard agent and the project's own test suite.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from mend.integrations.llm import LLMClient, LLMError, extract_json_object
from mend.swarm.recon import ReconReport


@dataclass
class Candidate:
    strategy: str
    rel_path: str
    original_source: str
    patched_source: str
    rationale: str

    @property
    def patch_hash(self) -> str:
        """SHA-256 of the full patched file: what the owner approves on-chain."""
        return hashlib.sha256(self.patched_source.encode("utf-8")).hexdigest()

    @property
    def diff(self) -> str:
        return "".join(
            difflib.unified_diff(
                self.original_source.splitlines(keepends=True),
                self.patched_source.splitlines(keepends=True),
                fromfile=f"a/{self.rel_path}",
                tofile=f"b/{self.rel_path}",
            )
        )

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data.pop("original_source")
        data["patch_hash"] = self.patch_hash
        data["diff"] = self.diff
        return data


@dataclass
class Proposal:
    """A forge agent's answer: a candidate, or the reason it has none."""

    strategy: str
    candidate: Optional[Candidate] = None
    declined: str = ""


def _line_offsets(source: str) -> List[int]:
    offsets, total = [], 0
    for line in source.splitlines(keepends=True):
        offsets.append(total)
        total += len(line.encode("utf-8"))
    return offsets


def _replace_node(source: str, node: ast.AST, replacement: str) -> str:
    """Replaces the exact source span of an AST node (AST columns are UTF-8 byte offsets)."""
    raw = source.encode("utf-8")
    offsets = _line_offsets(source)
    start = offsets[node.lineno - 1] + node.col_offset  # type: ignore[attr-defined]
    end = offsets[node.end_lineno - 1] + node.end_col_offset  # type: ignore[attr-defined]
    return (raw[:start] + replacement.encode("utf-8") + raw[end:]).decode("utf-8")


def _is_simple(node: ast.AST) -> bool:
    """True for expressions that are safe to evaluate twice: names, attributes, constant subscripts."""
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.Attribute):
        return _is_simple(node.value)
    if isinstance(node, ast.Subscript):
        index = node.slice
        return _is_simple(node.value) and isinstance(index, (ast.Constant, ast.Name))
    return False


def _nodes_on_line(tree: ast.AST, line: int) -> List[ast.AST]:
    return [
        n for n in ast.walk(tree)
        if getattr(n, "lineno", None) is not None and n.lineno <= line <= (getattr(n, "end_lineno", n.lineno) or n.lineno)
    ]


class RuleForge:
    """Deterministic repairs. Each rule returns (patched_source, rationale) or None."""

    strategy = "rules"

    def propose(self, recon: ReconReport, source: str) -> Proposal:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return Proposal(self.strategy, declined="Source does not parse")

        rules = {
            "ZeroDivisionError": self._zero_division,
            "KeyError": self._key_error,
            "AttributeError": self._none_attribute,
            "TypeError": self._none_attribute,
        }
        rule = rules.get(recon.error_type)
        if rule is None:
            return Proposal(self.strategy, declined=f"No deterministic rule for {recon.error_type}")
        result = rule(recon, source, tree)
        if result is None:
            return Proposal(self.strategy, declined=f"The {recon.error_type} rule does not match this statement")
        patched, rationale = result
        try:
            ast.parse(patched)
        except SyntaxError:
            return Proposal(self.strategy, declined="Rule produced code that does not parse")
        if patched == source:
            return Proposal(self.strategy, declined="Rule produced no change")
        return Proposal(self.strategy, Candidate(self.strategy, recon.rel_path, source, patched, rationale))

    def _zero_division(self, recon: ReconReport, source: str, tree: ast.AST) -> Optional[Tuple[str, str]]:
        divisions = [
            n for n in _nodes_on_line(tree, recon.line_number)
            if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Div, ast.FloorDiv, ast.Mod)) and _is_simple(n.right)
        ]
        if len(divisions) != 1:
            return None  # zero or several candidate divisions: ambiguous, so decline
        node = divisions[0]
        expr = ast.get_source_segment(source, node)
        denom = ast.get_source_segment(source, node.right)
        if not expr or not denom:
            return None
        fallback = "0.0" if isinstance(node.op, ast.Div) else "0"
        patched = _replace_node(source, node, f"({expr} if {denom} else {fallback})")
        return patched, f"Guard the division so a zero `{denom}` yields {fallback} instead of raising."

    def _key_error(self, recon: ReconReport, source: str, tree: ast.AST) -> Optional[Tuple[str, str]]:
        missing = recon.error_message.strip()
        lookups = [
            n for n in _nodes_on_line(tree, recon.line_number)
            if isinstance(n, ast.Subscript)
            and isinstance(n.ctx, ast.Load)
            and isinstance(n.slice, ast.Constant)
            and repr(n.slice.value) == missing
            and _is_simple(n.value)
        ]
        if len(lookups) != 1:
            return None
        node = lookups[0]
        container = ast.get_source_segment(source, node.value)
        key = ast.get_source_segment(source, node.slice)
        if not container or not key:
            return None
        patched = _replace_node(source, node, f"{container}.get({key})")
        return patched, f"Read the optional key {key} with .get() so a missing key yields None instead of raising."

    def _none_attribute(self, recon: ReconReport, source: str, tree: ast.AST) -> Optional[Tuple[str, str]]:
        if "'NoneType' object" not in recon.error_message:
            return None
        if recon.enclosing_scope == "module" or "def " not in recon.enclosing_scope:
            return None  # an early return needs a function to return from
        statement = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.stmt) and n.lineno == recon.line_number
             and not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))),
            None,
        )
        if statement is None:
            return None
        names: List[str] = []
        for n in ast.walk(statement):
            target = None
            if isinstance(n, ast.Attribute) and f"attribute '{n.attr}'" in recon.error_message:
                target = n.value
            elif isinstance(n, ast.Subscript) and "not subscriptable" in recon.error_message:
                target = n.value
            if isinstance(target, ast.Name) and target.id not in names:
                names.append(target.id)
        # Use the reported frame locals to confirm which name was actually None.
        confirmed = [n for n in names if recon.frame_locals.get(n) == "None"]
        if len(confirmed) != 1:
            return None
        name = confirmed[0]
        lines = source.splitlines(keepends=True)
        first = lines[statement.lineno - 1]
        indent = first[: len(first) - len(first.lstrip())]
        lines.insert(statement.lineno - 1, f"{indent}if {name} is None:\n{indent}    return None\n")
        return "".join(lines), f"Return None early when `{name}` is None instead of dereferencing it."


LLM_SYSTEM = """You are a senior engineer fixing one production crash with the smallest correct change.

Rules:
- Fix the root cause of the reported exception in the given file. Change nothing else.
- Do not swallow exceptions with bare `except` or `except Exception: pass`.
- Do not add imports of os, subprocess, socket or network libraries, and do not call eval or exec.
- Do not edit tests. If failing tests are shown, your change must make them pass.
- Keep the public behaviour of working code paths identical.

Reply with one JSON object and nothing else:
{"rationale": "<one sentence>", "edits": [{"search": "<exact text copied from the file>", "replace": "<new text>"}]}
Each "search" must match exactly one place in the file, including indentation."""


class LLMForge:
    strategy = "llm"

    def __init__(self, client: LLMClient) -> None:
        self.client = client

    @property
    def available(self) -> bool:
        return self.client.configured

    def propose(self, recon: ReconReport, source: str) -> Proposal:
        if not self.available:
            return Proposal(self.strategy, declined="No LLM API key configured")
        try:
            reply = self.client.complete(LLM_SYSTEM, self._prompt(recon, source))
        except LLMError as exc:
            return Proposal(self.strategy, declined=str(exc))

        data = extract_json_object(reply)
        if not data or not isinstance(data.get("edits"), list) or not data["edits"]:
            return Proposal(self.strategy, declined="Model reply contained no edits")

        patched = source
        for edit in data["edits"]:
            search = edit.get("search") if isinstance(edit, dict) else None
            replace = edit.get("replace") if isinstance(edit, dict) else None
            if not isinstance(search, str) or not isinstance(replace, str) or not search:
                return Proposal(self.strategy, declined="Model returned a malformed edit")
            occurrences = patched.count(search)
            if occurrences != 1:
                return Proposal(
                    self.strategy,
                    declined=f"Model edit matched {occurrences} places in the file; exactly one is required",
                )
            patched = patched.replace(search, replace, 1)

        if patched == source:
            return Proposal(self.strategy, declined="Model edits produced no change")
        try:
            ast.parse(patched)
        except SyntaxError as exc:
            return Proposal(self.strategy, declined=f"Model patch does not parse: {exc.msg}")
        rationale = str(data.get("rationale") or "").strip()[:300] or "Model-proposed fix."
        return Proposal(self.strategy, Candidate(self.strategy, recon.rel_path, source, patched, rationale))

    def _prompt(self, recon: ReconReport, source: str) -> str:
        lines = source.splitlines()
        if len(lines) <= 400:
            file_view = source
        else:
            start, end = max(0, recon.line_number - 150), min(len(lines), recon.line_number + 150)
            file_view = "\n".join(lines[start:end])
        parts = [
            f"File: {recon.rel_path}",
            f"Exception: {recon.error_type}: {recon.error_message}",
            f"Crash line {recon.line_number}, inside {recon.enclosing_scope}: {recon.failing_statement}",
        ]
        if recon.frame_locals:
            shown = "\n".join(f"  {k} = {v}" for k, v in list(recon.frame_locals.items())[:20])
            parts.append(f"Local variables at the crash (secrets redacted):\n{shown}")
        baseline = recon.baseline or {}
        if baseline.get("tests_failed"):
            parts.append(
                "Tests failing before any patch:\n" + (baseline.get("output") or "")[-2500:]
            )
        parts.append(f"File contents:\n```python\n{file_view}\n```")
        return "\n\n".join(parts)
