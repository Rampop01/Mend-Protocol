"""
Swarm orchestrator: runs one incident from crash report to settlement.

  crash report -> open incident on-chain (bounty reserved)
               -> recon locates and reproduces
               -> each forge agent proposes a patch
               -> guard screens statically, then both forge and guard run the tests
               -> best passing patch waits for a human
  approve      -> owner releases the bounty on-chain, patch is applied, PR opened
  reject       -> bounty returns to the treasury

Each agent's step is signed by that agent's own NEAR account. When NEAR is not
configured the pipeline still runs, and the incident says so instead of
inventing a receipt.
"""
from __future__ import annotations

import hashlib
import logging
import os
import queue
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from mend.config import Project, Settings
from mend.connect import ConnectError, sync_workspace
from mend.integrations.escrow import EscrowClient
from mend.integrations.github import GitHubClient
from mend.integrations.llm import LLMClient
from mend.integrations.near import NearError, TxResult, yocto_to_near
from mend.sandbox import TestRun, run_tests
from mend.store import IncidentStore
from mend.swarm.forge import Candidate, LLMForge, RuleForge
from mend.swarm.guard import GuardAgent, GuardReport
from mend.swarm.recon import ReconAgent, ReconReport, crash_fingerprint, locate_frame

logger = logging.getLogger("mend.swarm")

Listener = Callable[[Dict[str, Any], str], None]


class IncidentError(Exception):
    """A request that cannot be carried out; the message is safe to show to the caller."""


class SwarmOrchestrator:
    def __init__(
        self,
        settings: Settings,
        store: Optional[IncidentStore] = None,
        escrow: Optional[EscrowClient] = None,
        llm: Optional[LLMClient] = None,
        github: Optional[GitHubClient] = None,
        accounts: Optional[Any] = None,
    ) -> None:
        self.settings = settings
        self.store = store or IncidentStore(settings.db_path)
        self.escrow = escrow
        self.github = github or GitHubClient(token=settings.github_token)
        self.accounts = accounts  # AccountStore on a public service
        self.recon = ReconAgent(settings)
        self.guard = GuardAgent()
        self.rule_forge = RuleForge()
        self.llm_forge = LLMForge(llm or LLMClient(settings.llm_api_key, settings.llm_base_url, settings.llm_model))
        self.listeners: List[Listener] = []
        self._queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self._approval_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._recover_interrupted()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._work, name="mend-swarm", daemon=True)
            self._worker.start()

    def stop(self, timeout: float = 30.0) -> None:
        """Finishes the incident in progress, then stops the worker."""
        worker, self._worker = self._worker, None
        if worker is not None and worker.is_alive():
            self._queue.put(None)
            worker.join(timeout)

    def wait_idle(self) -> None:
        """Blocks until every queued incident has been analysed. Used by tests and the CLI."""
        self._queue.join()

    def _work(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:
                    return
                self._analyze(job)
            except Exception as exc:  # the worker must outlive any single incident
                logger.exception("Incident pipeline crashed")
                incident = self.store.get(job["incident_id"]) if job else None
                if incident:
                    self._fail(incident, f"Internal error during analysis: {exc}")
            finally:
                self._queue.task_done()

    def _recover_interrupted(self) -> None:
        for incident in self.store.with_status(["RECEIVED", "ANALYZING", "APPROVING"]):
            if incident["status"] == "APPROVING":
                # The approval may or may not have reached the chain: ask the chain.
                self._reconcile_approval(incident)
            else:
                self._fail(incident, "The server restarted while this incident was being analysed")

    # ------------------------------------------------------------------
    # Intake
    # ------------------------------------------------------------------

    def github_for(self, project: Project) -> GitHubClient:
        """A project connected by a user is accessed with that user's own GitHub token."""
        if project.owner_telegram_id and self.accounts is not None:
            token = self.accounts.github_token(project.owner_telegram_id)
            if not token:
                raise IncidentError("The owner of this project has disconnected GitHub")
            return GitHubClient(token=token)
        return self.github

    def report(self, payload: Dict[str, Any], project_hint: Optional[Project] = None) -> Dict[str, Any]:
        """Validates a crash report and queues it. Returns the incident record."""
        frames = payload.get("frames")
        if not isinstance(frames, list) or not frames:
            # Older SDK payloads carry a single location.
            frames = [{"file": payload.get("file_path"), "line": payload.get("line_number")}]
        error_type = str(payload.get("error_type") or "")[:120]
        if not error_type.isidentifier():
            raise IncidentError("error_type must be an exception class name")
        error_message = str(payload.get("error_message") or "")[:2000]

        if project_hint is not None:
            # A per-project SDK token may only report crashes in its own project.
            scoped = type(self.settings)(base_dir=self.settings.base_dir, projects=[project_hint])
            located = locate_frame(scoped, frames, repo_hint=project_hint.repo)
        else:
            located = locate_frame(self.settings, frames, repo_hint=payload.get("repo"))
        if located is None:
            raise IncidentError(
                "No traceback frame belongs to a registered project. "
                "Add the project to mend.projects.json on the Mend server."
            )
        project, file_path, line_number, frame = located
        rel_path = project.relative(file_path)
        fingerprint = crash_fingerprint(project.repo, rel_path, line_number, error_type)

        owner = project.owner_telegram_id
        chat_id = ""
        if owner and self.accounts is not None:
            user = self.accounts.get(owner)
            chat_id = user.chat_id if user else ""

        existing = self.store.find_open(fingerprint)
        if existing:
            existing["occurrences"] = existing.get("occurrences", 1) + 1
            existing["updated_at"] = time.time()
            self.store.save(existing)
            return existing
        if owner and self.accounts is not None and self.settings.public:
            if not self.accounts.take_quota(owner, "incident", self.settings.limit_incidents_per_day):
                raise IncidentError("Daily incident limit reached for this project's owner; try again tomorrow")

        raw_locals = frame.get("locals") or payload.get("frame_locals")
        frame_locals = (
            {str(k)[:80]: str(v)[:200] for k, v in list(raw_locals.items())[:40]} if isinstance(raw_locals, dict) else {}
        )
        now = time.time()
        incident_id = "INC-" + hashlib.sha256(f"{fingerprint}:{time.time_ns()}".encode()).hexdigest()[:10].upper()
        incident: Dict[str, Any] = {
            "incident_id": incident_id,
            "fingerprint": fingerprint,
            "status": "RECEIVED",
            "created_at": now,
            "updated_at": now,
            "occurrences": 1,
            "repo": project.repo,
            "rel_path": rel_path,
            "line_number": line_number,
            "error_type": error_type,
            "error_message": error_message,
            "traceback": str(payload.get("traceback") or "")[-6000:],
            "reported_line": str(frame.get("code") or "")[:300],
            "recon": None,
            "candidates": [],
            "winner": None,
            "bounty_near": self.settings.bounty_near,
            "chain": {"enabled": False, "contract_id": None, "error": None, "txs": []},
            "timeline": [],
            "resolution": None,
            "error": None,
            "telegram": None,
            "owner_telegram_id": owner,
            "chat_id": chat_id,
            "near_owner": project.near_owner,
            "pull_request": None,
        }
        self._note(incident, "sdk", f"Crash received: {error_type} at {rel_path}:{line_number}")
        self.store.save(incident)
        self._emit(incident, "received")
        self._queue.put({"incident_id": incident_id, "frame_locals": frame_locals})
        return incident

    # ------------------------------------------------------------------
    # Analysis pipeline
    # ------------------------------------------------------------------

    def _analyze(self, job: Dict[str, Any]) -> None:
        incident = self.store.get(job["incident_id"])
        if incident is None:
            return
        project = self.settings.project_by_repo(incident["repo"])
        if project is None:
            self._fail(incident, f"Project {incident['repo']} is no longer registered")
            return
        file_path = project.root / incident["rel_path"]
        incident["status"] = "ANALYZING"
        self.store.save(incident)

        if project.managed:
            # Analyse exactly what is on GitHub, so the fix applies cleanly as a pull request.
            try:
                commit = sync_workspace(project, token=self.github_for(project).token)
            except (ConnectError, IncidentError) as exc:
                self._fail(incident, f"Could not sync {project.repo} from GitHub: {exc}")
                return
            incident["commit"] = commit
            self._note(incident, "operator", f"Synced {project.repo}@{project.base_branch} ({commit[:7]}) from GitHub")
            if not file_path.is_file():
                self._fail(incident, f"{incident['rel_path']} does not exist on {project.base_branch}")
                return
            lines = file_path.read_text(encoding="utf-8").splitlines()
            current = lines[incident["line_number"] - 1].strip() if incident["line_number"] <= len(lines) else ""
            if incident.get("reported_line") and current != incident["reported_line"]:
                self._close_unpaid(
                    incident, "STALE",
                    f"Line {incident['line_number']} on {project.base_branch} is no longer the line that crashed, "
                    "so the running deployment is older than GitHub (it may already be fixed). Redeploy and "
                    "Mend will analyse any crash that remains.",
                )
                return

        # Reserve the bounty before any work is done, so agents work against funded escrow.
        if self.escrow is None:
            incident["chain"]["error"] = "NEAR is not configured on this server (run `mend setup`)"
            self._note(incident, "operator", "NEAR not configured: analysing without on-chain escrow")
        elif project.repo not in self.escrow.repos:
            incident["chain"]["error"] = f"No treasury registered for {project.repo}"
            self._note(incident, "operator", incident["chain"]["error"])
        else:
            incident["chain"]["contract_id"] = self.escrow.contract_id
            tx = self._chain(
                incident, "operator", "open_incident",
                lambda: self.escrow.open_incident(
                    incident["incident_id"], project.repo, incident["fingerprint"], incident["bounty_near"]
                ),
                f"Opened incident and reserved {incident['bounty_near']} NEAR bounty",
            )
            incident["chain"]["enabled"] = tx is not None
        self.store.save(incident)

        # Recon
        report = self.recon.analyze(
            project, file_path, incident["line_number"], incident["error_type"], incident["error_message"],
            frame_locals=job.get("frame_locals"),
        )
        incident["recon"] = report.to_dict()
        self._note(incident, "recon", report.summary)
        if not report.ok:
            self._fail(incident, report.summary)
            return
        if incident["chain"]["enabled"]:
            self._chain(
                incident, "recon", "submit_recon",
                lambda: self.escrow.submit_recon(incident["incident_id"], report.report_hash()),
                "Committed forensic report hash",
            )
        self.store.save(incident)
        self._emit(incident, "progress")

        # Forge agents compete; the guard reviews each proposal.
        source = file_path.read_text(encoding="utf-8")
        for forge in (self.rule_forge, self.llm_forge):
            entry = self._evaluate(incident, project, report, forge, source)
            incident["candidates"].append(entry)
            self.store.save(incident)
            self._emit(incident, "progress")

        passing = [c for c in incident["candidates"] if c.get("guard") and c["guard"]["passed"] and c["guard"]["executed"]]
        if not passing:
            self._close_unpaid(incident, "NO_FIX", "No candidate patch passed the guard and the test suite")
            return
        # Highest guard score wins; ties go to the smaller change, then to the deterministic agent.
        passing.sort(key=lambda c: (-c["guard"]["score"], c["guard"]["lines_added"] + c["guard"]["lines_removed"],
                                    c["strategy"] != "rules"))
        winner = passing[0]
        incident["winner"] = winner["strategy"]
        incident["status"] = "AWAITING_APPROVAL"
        self._note(
            incident, "guard",
            f"Selected the {winner['strategy']} patch (score {winner['guard']['score']}/100). Waiting for human approval.",
        )
        self.store.save(incident)
        self._emit(incident, "awaiting_approval")

    def _evaluate(self, incident: Dict[str, Any], project: Project, report: ReconReport, forge: Any, source: str) -> Dict[str, Any]:
        strategy: str = forge.strategy
        agent = f"forge-{strategy}"
        entry: Dict[str, Any] = {
            "strategy": strategy,
            "agent_account": self.escrow.forge_account_id(strategy) if self.escrow else None,
            "declined": "",
            "rationale": "",
            "patch_hash": None,
            "diff": "",
            "patched_source": None,
            "forge_run": None,
            "guard": None,
        }
        proposal = forge.propose(report, source)
        if proposal.candidate is None:
            entry["declined"] = proposal.declined
            self._note(incident, agent, f"No proposal: {proposal.declined}")
            return entry

        candidate: Candidate = proposal.candidate
        entry.update(
            rationale=candidate.rationale, patch_hash=candidate.patch_hash,
            diff=candidate.diff, patched_source=candidate.patched_source,
        )
        self._note(incident, agent, f"Proposed a patch: {candidate.rationale}")
        on_chain = incident["chain"]["enabled"]

        # Static gate first: a patch that fails it is never executed.
        screen = self.guard.screen(report, candidate)
        if not screen.passed:
            entry["guard"] = screen.to_dict()
            self._note(incident, "guard", f"{strategy} patch: {screen.summary}")
            if on_chain:
                self._chain(incident, agent, "submit_patch",
                            lambda: self.escrow.submit_patch(strategy, incident["incident_id"], candidate.patch_hash, 0, 0),
                            "Committed patch hash (not executed)")
                self._chain(incident, "guard", "attest",
                            lambda: self.escrow.attest(incident["incident_id"], strategy, candidate.patch_hash,
                                                       False, 0, screen.report_hash()),
                            f"Attested the {strategy} patch: blocked by static policy")
            return entry

        overlay = {candidate.rel_path: candidate.patched_source}
        forge_run: TestRun = run_tests(project, overlay)
        entry["forge_run"] = forge_run.to_dict()
        self._note(
            incident, agent,
            forge_run.error or f"Ran the suite on the patch: {forge_run.tests_run - forge_run.tests_failed}/{forge_run.tests_run} passed",
        )
        submitted = None
        if on_chain:
            submitted = self._chain(
                incident, agent, "submit_patch",
                lambda: self.escrow.submit_patch(strategy, incident["incident_id"], candidate.patch_hash,
                                                 forge_run.tests_run, forge_run.tests_failed),
                "Committed patch hash and test evidence",
            )

        # The guard runs the suite itself instead of trusting the forge agent's numbers.
        guard_run: TestRun = run_tests(project, overlay)
        verdict: GuardReport = self.guard.verify(report, candidate, screen, forge_run, guard_run)
        entry["guard"] = verdict.to_dict()
        self._note(incident, "guard", f"{strategy} patch: {verdict.summary}")
        if on_chain and submitted is not None:
            attested = self._chain(
                incident, "guard", "attest",
                lambda: self.escrow.attest(incident["incident_id"], strategy, candidate.patch_hash,
                                           verdict.passed, verdict.score, verdict.report_hash()),
                f"Attested the {strategy} patch: {'pass' if verdict.passed else 'fail'}, score {verdict.score}",
            )
            entry["attested_on_chain"] = attested is not None
        return entry

    # ------------------------------------------------------------------
    # Human decision
    # ------------------------------------------------------------------

    def approve(self, incident_id: str, approver: str) -> Dict[str, Any]:
        with self._approval_lock:
            incident = self.store.get(incident_id)
            if incident is None:
                raise IncidentError(f"Incident {incident_id} not found")
            if incident["status"] != "AWAITING_APPROVAL":
                raise IncidentError(f"Incident {incident_id} is {incident['status']}, not awaiting approval")
            project = self.settings.project_by_repo(incident["repo"])
            if project is None:
                raise IncidentError(f"Project {incident['repo']} is no longer registered")
            if incident.get("near_owner") and incident["chain"]["enabled"]:
                raise IncidentError(f"This treasury belongs to {incident['near_owner']}: approve by signing with that NEAR wallet")
            winner = next(c for c in incident["candidates"] if c["strategy"] == incident["winner"])
            target = project.root / incident["rel_path"]

            current = (
                hashlib.sha256(target.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
                if target.exists() else ""
            )
            if current != incident["recon"]["source_sha256"]:
                raise IncidentError(
                    f"{incident['rel_path']} changed since the patch was verified. Reject this incident; "
                    "a new one will be analysed against the current code."
                )

            incident["status"] = "APPROVING"
            self.store.save(incident)
            resolution: Dict[str, Any] = {
                "approver": approver, "strategy": winner["strategy"], "patch_hash": winner["patch_hash"],
                "settlement": None, "payouts": [], "applied": False, "pr_url": None, "notes": [],
            }
            incident["resolution"] = resolution

            # Connected repositories receive the fix as a pull request, opened before any money moves.
            # If the payout then fails, the PR is kept and a retry reuses it.
            github = self.github_for(project)
            if project.managed:
                if not incident.get("pull_request"):
                    if not github.token:
                        self._abort_approval(incident)
                        raise IncidentError("Cannot open a pull request: no GitHub token on the Mend server")
                    result = github.open_fix_pull_request(
                        repo=project.repo, base_branch=project.base_branch,
                        branch=f"mend/{incident_id.lower()}", rel_path=incident["rel_path"],
                        original_sha256=incident["recon"]["source_sha256"],
                        patched_source=winner["patched_source"],
                        title=f"fix: {incident['error_type']} in {incident['rel_path']} ({incident_id})",
                        body=self._pr_body(incident, winner, resolution),
                    )
                    if not result.get("ok"):
                        self._note(incident, "operator", f"Pull request not opened: {result.get('error')}")
                        self._abort_approval(incident)
                        raise IncidentError(f"Pull request could not be opened, nothing was paid: {result.get('error')}")
                    incident["pull_request"] = {"url": result["pr_url"], "number": result["pr_number"],
                                                "branch": result["branch"]}
                    self._note(incident, "operator", f"Opened pull request #{result['pr_number']}",
                               tx_url=None)
                    self.store.save(incident)
                resolution["pr_url"] = incident["pull_request"]["url"]

            # 1. Settle on-chain first. If the contract refuses, nothing is changed locally.
            if incident["chain"]["enabled"]:
                if not winner.get("attested_on_chain"):
                    self._abort_approval(incident)
                    raise IncidentError("The winning patch has no on-chain guard attestation, so it cannot be paid")
                try:
                    tx = self.escrow.approve(incident_id, winner["strategy"], winner["patch_hash"])
                except NearError as exc:
                    self._note(incident, "owner", f"On-chain approval failed: {exc}")
                    self._abort_approval(incident)
                    kept = " The pull request stays open; approve again to retry the payout." if incident.get("pull_request") else ""
                    raise IncidentError(f"On-chain approval failed, nothing was paid: {exc}.{kept}")
                self._record_tx(incident, "owner", "approve", tx,
                                f"{approver} approved; {incident['bounty_near']} NEAR bounty released")
                resolution["settlement"] = tx.to_dict()
                resolution["payouts"] = self._read_payouts(incident_id)
            else:
                resolution["notes"].append(
                    f"No on-chain settlement: {incident['chain']['error'] or 'the bounty was never reserved'}"
                )
            self.store.save(incident)

            if project.managed:
                self._comment_settlement(incident, project, resolution)
            else:
                # A local project: apply the verified patch to the working tree, then offer a PR.
                self._write_atomic(target, winner["patched_source"])
                resolution["applied"] = True
                self._note(incident, "operator", f"Applied the patch to {incident['rel_path']}")
                self._open_pull_request(incident, project, winner, resolution)

            incident["status"] = "RESOLVED"
            self.store.save(incident)
            self._emit(incident, "resolved")
            return incident

    def finalize_wallet_approval(self, incident_id: str, account_id: str, tx_hash: str = "") -> Dict[str, Any]:
        """
        The repository owner signed `approve` with their own wallet. Check the chain, then deliver the fix.
        Nothing here moves money: the contract already paid, or this raises.
        """
        with self._approval_lock:
            incident = self.store.get(incident_id)
            if incident is None:
                raise IncidentError(f"Incident {incident_id} not found")
            if incident["status"] == "RESOLVED":
                return incident
            if incident["status"] != "AWAITING_APPROVAL":
                raise IncidentError(f"Incident {incident_id} is {incident['status']}")
            if not incident.get("near_owner") or incident["near_owner"] != account_id:
                raise IncidentError("This incident's treasury belongs to a different NEAR account")
            project = self.settings.project_by_repo(incident["repo"])
            if project is None or self.escrow is None:
                raise IncidentError(f"Project {incident['repo']} is no longer registered")
            winner = next(c for c in incident["candidates"] if c["strategy"] == incident["winner"])
            on_chain = self.escrow.get_incident(incident_id) or {}
            if on_chain.get("status") != "Resolved":
                raise IncidentError("The approval is not on-chain yet. Wait a few seconds and try again.")
            if (on_chain.get("winner") != self.escrow.forge_account_id(winner["strategy"])
                    or on_chain.get("accepted_patch_hash") != winner["patch_hash"]):
                raise IncidentError("The on-chain approval does not match the patch Mend verified")

            resolution: Dict[str, Any] = {
                "approver": account_id, "strategy": winner["strategy"], "patch_hash": winner["patch_hash"],
                "settlement": {"tx_hash": tx_hash, "explorer_url": self.escrow.rpc.tx_url(tx_hash) if tx_hash
                               else self.escrow.rpc.account_url(self.escrow.contract_id), "signer_id": account_id},
                "payouts": self._read_payouts(incident_id), "applied": False, "pr_url": None,
                "notes": [f"Payout signed by {account_id} with their own NEAR wallet"],
            }
            incident["resolution"] = resolution
            incident["chain"]["txs"].append({"step": "approve", "agent": "owner", "signer": account_id,
                                             "tx_hash": tx_hash, "explorer_url": resolution["settlement"]["explorer_url"]})
            self._note(incident, "owner", f"{account_id} approved with their NEAR wallet; {incident['bounty_near']} NEAR released",
                       tx_url=resolution["settlement"]["explorer_url"])
            if project.managed:
                github = self.github_for(project)
                result = github.open_fix_pull_request(
                    repo=project.repo, base_branch=project.base_branch, branch=f"mend/{incident_id.lower()}",
                    rel_path=incident["rel_path"], original_sha256=incident["recon"]["source_sha256"],
                    patched_source=winner["patched_source"],
                    title=f"fix: {incident['error_type']} in {incident['rel_path']} ({incident_id})",
                    body=self._pr_body(incident, winner, resolution),
                )
                if result.get("ok"):
                    incident["pull_request"] = {"url": result["pr_url"], "number": result["pr_number"], "branch": result["branch"]}
                    resolution["pr_url"] = result["pr_url"]
                    self._note(incident, "operator", f"Opened pull request #{result['pr_number']}")
                else:
                    resolution["notes"].append(f"The bounty was paid, but the pull request failed: {result.get('error')}")
            incident["status"] = "RESOLVED"
            self.store.save(incident)
            self._emit(incident, "resolved")
            return incident

    def reject(self, incident_id: str, rejector: str) -> Dict[str, Any]:
        with self._approval_lock:
            incident = self.store.get(incident_id)
            if incident is None:
                raise IncidentError(f"Incident {incident_id} not found")
            if incident["status"] != "AWAITING_APPROVAL":
                raise IncidentError(f"Incident {incident_id} is {incident['status']}, not awaiting approval")
            self._close_unpaid(incident, "REJECTED", f"Rejected by {rejector}")
            return incident

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _abort_approval(self, incident: Dict[str, Any]) -> None:
        incident["status"] = "AWAITING_APPROVAL"
        incident["resolution"] = None
        self.store.save(incident)

    def _comment_settlement(self, incident: Dict[str, Any], project: Project, resolution: Dict[str, Any]) -> None:
        """Adds the on-chain settlement to the pull request, so reviewers can audit the payout."""
        pr = incident["pull_request"]
        if resolution.get("settlement"):
            lines = [f"Approved by {resolution['approver']}. Bounty settled on NEAR: {resolution['settlement']['explorer_url']}", ""]
            lines += [f"- {p['role']}: {p['amount_near']} NEAR to `{p['account']}`" for p in resolution["payouts"]]
        else:
            lines = [f"Approved by {resolution['approver']}. " + " ".join(resolution["notes"])]
        result = self.github_for(project).comment(project.repo, pr["number"], "\n".join(lines))
        if not result.get("ok"):
            resolution["notes"].append(f"Could not comment on the pull request: {result.get('error')}")

    def _open_pull_request(self, incident: Dict[str, Any], project: Project, winner: Dict[str, Any],
                           resolution: Dict[str, Any]) -> None:
        if not self.github.token:
            resolution["notes"].append("No pull request: GITHUB_TOKEN is not set")
            return
        result = self.github.open_fix_pull_request(
            repo=project.repo,
            base_branch=project.base_branch,
            branch=f"mend/{incident['incident_id'].lower()}",
            rel_path=incident["rel_path"],
            original_sha256=incident["recon"]["source_sha256"],
            patched_source=winner["patched_source"],
            title=f"fix: {incident['error_type']} in {incident['rel_path']} ({incident['incident_id']})",
            body=self._pr_body(incident, winner, resolution),
        )
        if result.get("ok"):
            resolution["pr_url"] = result["pr_url"]
            self._note(incident, "operator", f"Opened pull request {result['pr_url']}")
        else:
            resolution["notes"].append(f"No pull request: {result.get('error')}")
            self._note(incident, "operator", f"Pull request not opened: {result.get('error')}")

    def _pr_body(self, incident: Dict[str, Any], winner: Dict[str, Any], resolution: Dict[str, Any]) -> str:
        guard = winner["guard"]
        run = winner["forge_run"]
        lines = [
            f"Automated fix for `{incident['error_type']}: {incident['error_message']}` "
            f"at `{incident['rel_path']}:{incident['line_number']}`.",
            "",
            f"**What changed:** {winner['rationale']}",
            "",
            "**Verification**",
            f"- Tests with the patch: {run['tests_run'] - run['tests_failed']}/{run['tests_run']} passed",
        ]
        baseline = incident["recon"].get("baseline") or {}
        if baseline.get("tests_failed"):
            lines.append(f"- Tests before the patch: {baseline['tests_failed']} of {baseline['tests_run']} failed")
        lines.append(f"- Guard score: {guard['score']}/100")
        lines += [f"  - {c['name']}: {c['status']} ({c['detail']})" for c in guard["checks"]]
        lines += ["", f"Approved by {resolution['approver']}. Patch SHA-256: `{winner['patch_hash']}`"]
        if resolution.get("settlement"):
            lines.append(f"Bounty settlement on NEAR: {resolution['settlement']['explorer_url']}")
        return "\n".join(lines)

    def _read_payouts(self, incident_id: str) -> List[Dict[str, Any]]:
        try:
            on_chain = self.escrow.get_incident(incident_id) or {}
        except NearError as exc:
            logger.warning("Could not read payouts for %s: %s", incident_id, exc)
            return []
        return [
            {"account": p["account"], "role": p["role"], "amount_near": yocto_to_near(p["amount"])}
            for p in on_chain.get("payouts", [])
        ]

    def _reconcile_approval(self, incident: Dict[str, Any]) -> None:
        """After a restart mid-approval, the chain is the source of truth."""
        status = None
        if self.escrow and incident["chain"]["enabled"]:
            try:
                status = (self.escrow.get_incident(incident["incident_id"]) or {}).get("status")
            except NearError:
                status = None
        if status == "Resolved":
            incident["status"] = "RESOLVED"
            resolution = incident.get("resolution") or {}
            resolution.setdefault("notes", []).append(
                "The server restarted during approval. The bounty was paid on-chain; "
                "check that the patch was applied to the working tree."
            )
            resolution["payouts"] = self._read_payouts(incident["incident_id"])
            incident["resolution"] = resolution
        else:
            incident["status"] = "AWAITING_APPROVAL"
            incident["resolution"] = None
        self.store.save(incident)

    def _close_unpaid(self, incident: Dict[str, Any], status: str, reason: str) -> None:
        if incident["chain"]["enabled"]:
            self._chain(incident, "operator", "reject",
                        lambda: self.escrow.reject(incident["incident_id"]),
                        "Closed the incident; bounty returned to the treasury")
        incident["status"] = status
        if status == "FAILED":
            incident["error"] = reason
        self._note(incident, "operator", reason)
        self.store.save(incident)
        self._emit(incident, status.lower())

    def _fail(self, incident: Dict[str, Any], reason: str) -> None:
        self._close_unpaid(incident, "FAILED", reason)

    def _chain(self, incident: Dict[str, Any], agent: str, step: str, send: Callable[[], TxResult], message: str) -> Optional[TxResult]:
        """Sends one transaction and records it. A failure is recorded, never hidden."""
        try:
            tx = send()
        except NearError as exc:
            incident["chain"]["error"] = f"{step} failed: {exc}"
            self._note(incident, agent, f"On-chain {step} failed: {exc}")
            logger.warning("%s: on-chain %s failed: %s", incident["incident_id"], step, exc)
            return None
        self._record_tx(incident, agent, step, tx, message)
        return tx

    def _record_tx(self, incident: Dict[str, Any], agent: str, step: str, tx: TxResult, message: str) -> None:
        incident["chain"]["txs"].append({
            "step": step, "agent": agent, "signer": tx.signer_id, "tx_hash": tx.tx_hash, "explorer_url": tx.explorer_url,
        })
        self._note(incident, agent, message, tx_url=tx.explorer_url)

    def _note(self, incident: Dict[str, Any], agent: str, message: str, tx_url: Optional[str] = None) -> None:
        entry: Dict[str, Any] = {"at": time.time(), "agent": agent, "message": message}
        if tx_url:
            entry["tx_url"] = tx_url
        incident["timeline"].append(entry)
        incident["updated_at"] = entry["at"]
        logger.info("%s [%s] %s", incident["incident_id"], agent, message)

    def _emit(self, incident: Dict[str, Any], event: str) -> None:
        for listener in self.listeners:
            try:
                listener(incident, event)
            except Exception:
                logger.exception("Incident listener failed")
        # Listeners may attach delivery details (for example a Telegram message id).
        self.store.save(incident)

    @staticmethod
    def _write_atomic(target: Path, content: str) -> None:
        mode = target.stat().st_mode
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".mend")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.chmod(tmp, mode)
            os.replace(tmp, target)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def public_view(self, incident: Dict[str, Any], full: bool = True) -> Dict[str, Any]:
        """The incident as exposed over the API: no full file contents."""
        view = {k: v for k, v in incident.items() if k not in ("telegram",)}
        view["candidates"] = [
            {k: v for k, v in c.items() if k != "patched_source"} for c in incident.get("candidates", [])
        ]
        if not full:
            for key in ("traceback", "recon", "timeline"):
                view.pop(key, None)
            for c in view["candidates"]:
                c.pop("diff", None)
                c.pop("forge_run", None)
        return view
