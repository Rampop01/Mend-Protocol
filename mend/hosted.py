"""
Public Mend service: what a Telegram user can do without any setup.

  connect_github  sign in to GitHub with a device code
  try_demo        fork the demo shop into the user's GitHub, connect it, and
                  trigger its real checkout crash, so the full loop (fix,
                  approval, NEAR payout, pull request) runs on their own fork
  watch           connect one of the user's own repositories and hand out an
                  SDK token for their application
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from mend.accounts import AccountError, AccountStore, DeviceFlow
from mend.config import Project, Settings
from mend.connect import ConnectError, connect_repo, normalize_repo
from mend.integrations.github import GitHubClient
from mend.integrations.near import NearError, yocto_to_near
from mend.sandbox import run_in_copy
from mend.swarm.orchestrator import IncidentError, SwarmOrchestrator

logger = logging.getLogger("mend.hosted")

DEMO_UPSTREAM = "Rampop01/mend-demo-shop"
PAYLOAD_MARKER = "MEND_PAYLOAD:"
DEMO_CRASH = """
import json, sys
sys.path.insert(0, ".")
from mend.sdk.watcher import extract_incident_context
from shop.pricing import quote
try:
    print("Checkout with coupon SAVEBIG:", quote([{"sku": "tee"}], coupon="SAVEBIG"))
except Exception as exc:
    print("%s" + json.dumps(extract_incident_context(exc, repo=sys.argv[1])))
""" % PAYLOAD_MARKER

Notify = Callable[[str], None]


class ServiceError(Exception):
    """Shown to the user as-is."""


class HostedService:
    def __init__(self, settings: Settings, orchestrator: SwarmOrchestrator, accounts: AccountStore) -> None:
        self.settings = settings
        self.orchestrator = orchestrator
        self.accounts = accounts
        self._busy: Dict[str, str] = {}
        self.clone_url_for: Optional[Callable[[str], str]] = None  # tests point clones at a local remote
        self._lock = threading.Lock()

    # -- helpers -----------------------------------------------------------

    def _claim(self, telegram_id: str, action: str) -> None:
        with self._lock:
            if telegram_id in self._busy:
                raise ServiceError(f"Still working on your last request ({self._busy[telegram_id]}). One moment.")
            self._busy[telegram_id] = action

    def _release(self, telegram_id: str) -> None:
        with self._lock:
            self._busy.pop(telegram_id, None)

    def _github(self, telegram_id: str) -> GitHubClient:
        token = self.accounts.github_token(telegram_id)
        if not token:
            raise ServiceError("Connect GitHub first: send /connect")
        return GitHubClient(token=token)

    def projects_of(self, telegram_id: str) -> List[Project]:
        return [p for p in self.settings.projects if p.owner_telegram_id == telegram_id]

    def _connect(self, telegram_id: str, repo: str, github: GitHubClient, notify: Notify) -> Dict[str, Any]:
        known = {p.repo.lower() for p in self.projects_of(telegram_id)}
        if repo.lower() not in known and len(known) >= self.settings.limit_repos_per_user:
            raise ServiceError(f"You can connect up to {self.settings.limit_repos_per_user} repositories. "
                               "Send /repos to see them.")
        try:
            result = connect_repo(self.settings, repo, github=github, owner_telegram_id=telegram_id,
                                  clone_url=self.clone_url_for(repo) if self.clone_url_for else None,
                                  log=lambda line: logger.info("[%s] %s", telegram_id, line))
        except ConnectError as exc:
            raise ServiceError(str(exc))
        except NearError as exc:
            raise ServiceError(f"Your repository is connected, but opening its NEAR treasury failed: {exc}")
        escrow = self.orchestrator.escrow
        if escrow is not None and result["treasury"] and result["repo"] not in escrow.repos:
            escrow.repos.append(result["repo"])
        return result

    def run_async(self, telegram_id: str, action: str, work: Callable[[], None], notify: Notify) -> None:
        self._claim(telegram_id, action)

        def target() -> None:
            try:
                work()
            except (ServiceError, AccountError, IncidentError) as exc:
                notify(f"⚠️ {exc}")
            except Exception:
                logger.exception("%s failed for %s", action, telegram_id)
                notify("⚠️ Something went wrong on the Mend server. Please try again in a minute.")
            finally:
                self._release(telegram_id)

        threading.Thread(target=target, daemon=True).start()

    # -- GitHub sign-in ----------------------------------------------------

    def connect_github(self, telegram_id: str, notify: Notify, show_code: Callable[[str, str], None]) -> None:
        def work() -> None:
            flow = DeviceFlow(self.settings.github_client_id)
            start = flow.start()
            show_code(start["user_code"], start["verification_uri"])
            token = flow.wait(start["device_code"], int(start.get("interval", 5)), int(start.get("expires_in", 900)))
            me = GitHubClient(token=token).user()
            if not me.get("ok"):
                raise ServiceError(f"GitHub sign-in worked, but reading your profile failed: {me.get('error')}")
            self.accounts.set_github(telegram_id, me["login"], token)
            notify(f"✅ Connected to GitHub as <b>{me['login']}</b>.\n\n"
                   "Send /try to see Mend fix a real bug on your own fork of the demo shop, "
                   "or /watch owner/repo to protect one of your repositories.")

        self.run_async(telegram_id, "GitHub sign-in", work, notify)

    # -- /try --------------------------------------------------------------

    def try_demo(self, telegram_id: str, notify: Notify) -> None:
        github = self._github(telegram_id)
        if not self.accounts.take_quota(telegram_id, "try", self.settings.limit_tries_per_day):
            raise ServiceError(f"You've used /try {self.settings.limit_tries_per_day} times today. Try again tomorrow.")

        def work() -> None:
            account = self.accounts.get(telegram_id)
            if account and account.github_login.lower() == DEMO_UPSTREAM.split("/")[0].lower():
                # GitHub cannot fork a repository into its owner's account; the owner uses the original.
                repo = DEMO_UPSTREAM
                notify(f"1/4 You own <b>{DEMO_UPSTREAM}</b>, so Mend will use it directly…")
            else:
                notify(f"1/4 Forking <b>{DEMO_UPSTREAM}</b> into your GitHub…")
                fork = github.fork(DEMO_UPSTREAM)
                if not fork.get("ok"):
                    raise ServiceError(f"Could not fork the demo shop: {fork.get('error')}")
                repo = fork["full_name"]
            for _ in range(20):  # a new fork takes a few seconds to appear
                if github.get_repo(repo).get("ok"):
                    break
                time.sleep(3)

            notify(f"2/4 Connecting <b>{repo}</b>: copying it, running its tests, opening a NEAR treasury…")
            result = None
            for attempt in range(4):
                try:
                    result = self._connect(telegram_id, repo, github, notify)
                    break
                except ServiceError as exc:
                    if "Cannot open" in str(exc) or "clone failed" in str(exc):
                        time.sleep(5)
                        continue
                    raise
            if result is None:
                raise ServiceError("GitHub is still preparing your fork. Send /try again in a minute.")
            project: Project = result["project"]
            treasury = result["treasury"]
            funds = (f"Treasury: {yocto_to_near(treasury['balance'])} NEAR on testnet."
                     if treasury else "No NEAR treasury (NEAR is not configured on this server).")
            notify(f"{result['tests_run'] - result['tests_failed']}/{result['tests_run']} tests pass on your fork. {funds}")

            notify("3/4 A customer checks out with the coupon <code>SAVEBIG</code>…")
            proc = run_in_copy(project, [sys.executable, "-c", DEMO_CRASH, project.repo], timeout=60)
            line = next((l for l in proc.stdout.splitlines() if l.startswith(PAYLOAD_MARKER)), None)
            if line is None:
                raise ServiceError(
                    "Checkout did not crash on your fork, so there is nothing to fix. If you merged an earlier "
                    "Mend fix, delete your fork of mend-demo-shop on GitHub and send /try again."
                )
            incident = self.orchestrator.report(json.loads(line[len(PAYLOAD_MARKER):]), project_hint=project)
            notify(f"💥 Checkout crashed: <code>{incident['error_type']}: {incident['error_message']}</code>\n\n"
                   f"4/4 Incident <code>{incident['incident_id']}</code> opened. The agents are working; "
                   "your fix card will appear here in about 30 seconds.")

        self.run_async(telegram_id, "/try", work, notify)

    # -- /watch ------------------------------------------------------------

    def watch(self, telegram_id: str, repo_arg: str, notify: Notify) -> None:
        github = self._github(telegram_id)
        try:
            repo = normalize_repo(repo_arg)
        except ConnectError as exc:
            raise ServiceError(str(exc))

        def work() -> None:
            notify(f"Connecting <b>{repo}</b>: copying it and running its tests…")
            result = self._connect(telegram_id, repo, github, notify)
            endpoint = (self.settings.public_url or "https://YOUR-MEND-SERVER") + "/api/v1/incidents"
            notify(
                f"✅ <b>{result['repo']}</b> is protected.\n"
                f"Tests: {result['tests_run'] - result['tests_failed']}/{result['tests_run']} pass on "
                f"{result['base_branch']} @ {result['commit'][:7]}.\n\n"
                "Add this to your Python app (pip install git+https://github.com/Rampop01/Mend-Protocol):\n"
                f"<pre>from mend.sdk import init_mend\ninit_mend(\n    endpoint=\"{endpoint}\",\n"
                f"    token=\"{result['ingest_token']}\",\n    repo=\"{result['repo']}\",\n)</pre>\n"
                "Keep the token private: it lets an app report crashes for this repository only. "
                "When your app crashes, the fix comes here for your approval and arrives as a pull request."
            )

        self.run_async(telegram_id, "/watch", work, notify)

    # -- status ------------------------------------------------------------

    def status_lines(self, telegram_id: str) -> List[str]:
        user = self.accounts.get(telegram_id)
        lines = [f"GitHub: <b>{user.github_login}</b>" if user and user.github_login else "GitHub: not connected (/connect)"]
        escrow = self.orchestrator.escrow
        for project in self.projects_of(telegram_id):
            line = f"• <b>{project.repo}</b>"
            if escrow is not None and project.repo in escrow.repos:
                try:
                    repo = escrow.get_repo(project.repo) or {}
                    line += (f": {yocto_to_near(repo.get('balance', 0))} NEAR free, "
                             f"{yocto_to_near(repo.get('total_paid', 0))} paid to agents")
                except NearError:
                    line += ": treasury unreachable right now"
            lines.append(line)
        if len(lines) == 1:
            lines.append("No repositories yet. Send /try or /watch owner/repo.")
        return lines
