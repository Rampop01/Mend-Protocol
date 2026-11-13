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
import secrets
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from mend.accounts import AccountError, AccountStore, DeviceFlow
from mend.config import Project, Settings
from mend.connect import ConnectError, connect_repo, normalize_repo
from mend.integrations.github import GitHubClient
from mend.integrations.near import NearError, near_to_yocto, yocto_to_near
from mend.links import make_token
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

FAUCET_URL = "https://docs.near.org/getting-started/faucet"
WALLET_DEPOSIT_NEAR = "0.2"


class ServiceError(Exception):
    """Shown to the user as-is."""


class HostedService:
    def __init__(self, settings: Settings, orchestrator: SwarmOrchestrator, accounts: AccountStore) -> None:
        self.settings = settings
        self.orchestrator = orchestrator
        self.accounts = accounts
        self._busy: Dict[str, str] = {}
        self.clone_url_for: Optional[Callable[[str], str]] = None  # tests point clones at a local remote
        # Set by the Telegram bot: send(chat_id, text, keyboard)
        self.send: Callable[..., Any] = lambda chat_id, text, keyboard=None: None
        self._fund_choices: Dict[str, Dict[str, Any]] = {}
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
            code = self.pending_code(telegram_id)
            if code:
                raise ServiceError(f"Finish connecting GitHub first: open github.com/login/device and enter "
                                   f"<code>{code}</code>. Send /connect for a new code.")
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
                                  register_on_chain=False,
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
        """Starts a GitHub sign-in. A new /connect replaces any sign-in still waiting."""
        flow = DeviceFlow(self.settings.github_client_id)
        start = flow.start()
        self.accounts.save_pending(telegram_id, start["device_code"], start["user_code"],
                                   int(start.get("interval", 5)), int(start.get("expires_in", 900)))
        show_code(start["user_code"], start["verification_uri"])
        self._wait_for_login(telegram_id, start["device_code"], int(start.get("interval", 5)),
                             int(start.get("expires_in", 900)), notify)

    def _wait_for_login(self, telegram_id: str, device_code: str, interval: int, expires_in: int, notify: Notify) -> None:
        def still_wanted() -> bool:
            return any(p["device_code"] == device_code for p in self.accounts.pending(telegram_id))

        def target() -> None:
            try:
                token = DeviceFlow(self.settings.github_client_id).wait(device_code, interval, expires_in, still_wanted)
                me = GitHubClient(token=token).user()
                if not me.get("ok"):
                    raise ServiceError(f"GitHub sign-in worked, but reading your profile failed: {me.get('error')}")
                self.accounts.set_github(telegram_id, me["login"], token)
                self.accounts.clear_pending(telegram_id, device_code)
                notify(f"✅ Connected to GitHub as <b>{me['login']}</b>.\n\n"
                       "Send /try to see Mend fix a real bug on your own fork of the demo shop, "
                       "or /watch owner/repo to protect one of your repositories.")
            except AccountError as exc:
                if str(exc) != "superseded":
                    self.accounts.clear_pending(telegram_id, device_code)
                    notify(f"⚠️ {exc}")
            except Exception:
                logger.exception("GitHub sign-in failed for %s", telegram_id)
                notify("⚠️ GitHub sign-in failed on the Mend server. Send /connect to try again.")

        threading.Thread(target=target, daemon=True).start()

    def resume_logins(self, notify_for: Callable[[str], Notify]) -> int:
        """After a restart, keeps waiting for sign-ins that were in progress."""
        pending = self.accounts.pending()
        for p in pending:
            self._wait_for_login(p["telegram_id"], p["device_code"], p["interval"], p["expires_in"],
                                 notify_for(p["telegram_id"]))
        return len(pending)

    def pending_code(self, telegram_id: str) -> Optional[str]:
        pending = self.accounts.pending(telegram_id)
        return pending[0]["user_code"] if pending else None

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
            notify(f"{result['tests_run'] - result['tests_failed']}/{result['tests_run']} tests pass on your fork.")
            self.offer_funding(telegram_id, project, then="try")

        self.run_async(telegram_id, "/try", work, notify)

    def crash_demo(self, telegram_id: str, project: Project, notify: Notify) -> None:
        """Runs the demo shop's checkout with the bad coupon, in the sandbox, and reports the crash."""
        notify("3/4 A customer checks out with the coupon <code>SAVEBIG</code>…")
        proc = run_in_copy(project, [sys.executable, "-c", DEMO_CRASH, project.repo], timeout=60)
        line = next((l for l in proc.stdout.splitlines() if l.startswith(PAYLOAD_MARKER)), None)
        if line is None:
            raise ServiceError(
                "Checkout did not crash on your fork, so there is nothing to fix. If you merged an earlier "
                "Mend fix, delete your fork of mend-demo-shop on GitHub and send /try again."
            )
        payload = json.loads(line[len(PAYLOAD_MARKER):])
        # Sent before reporting, so it appears above the incident card that the report creates.
        notify(f"💥 Checkout crashed: <code>{payload['error_type']}: {payload['error_message']}</code>\n\n"
               "4/4 Mend caught it. The agents are working; the card below updates as they go (about 30 seconds).")
        self.orchestrator.report(payload, project_hint=project)

    # -- Funding the bounty treasury ---------------------------------------

    def wallet_link(self, payload: Dict[str, Any], ttl: int = 3600) -> str:
        return f"{self.settings.public_url}/wallet?t={make_token(self.settings.secret_key, payload, ttl)}"

    def offer_funding(self, telegram_id: str, project: Project, then: str) -> None:
        """Lets the user fund their repository's treasury from their own wallet, or have Mend fund it."""
        user = self.accounts.get(telegram_id)
        chat_id = user.chat_id if user else telegram_id
        escrow = self.orchestrator.escrow
        if escrow is None:
            self.send(chat_id, "NEAR is not configured on this server, so this repository has no bounty treasury.")
            self._continue(telegram_id, project, then)
            return
        existing = escrow.get_repo(project.repo)
        if existing:  # funded earlier: keep whoever owns it
            if project.repo not in escrow.repos:
                escrow.repos.append(project.repo)
            if existing["owner"] != escrow.account_id("owner"):
                project.near_owner = existing["owner"]
                self._save(project)
                if int(existing["balance"]) < near_to_yocto(self.settings.bounty_near):
                    link = self.wallet_link({"k": "fund", "u": telegram_id, "r": project.repo, "then": then})
                    self.send(chat_id, (f"💰 The treasury for <b>{project.repo}</b> (owned by <code>{existing['owner']}</code>) "
                                        f"has {yocto_to_near(existing['balance'])} NEAR, less than one "
                                        f"{self.settings.bounty_near} NEAR bounty. Top it up from your wallet to continue."),
                              [[{"text": "🔐 Top up from my wallet", "url": link}]])
                    return
            # A Mend-funded treasury that ran low is topped up when the incident opens.
            self._continue(telegram_id, project, then)
            return
        choice = secrets.token_urlsafe(6)
        self._fund_choices[choice] = {"telegram_id": telegram_id, "repo": project.repo, "then": then}
        link = self.wallet_link({"k": "fund", "u": telegram_id, "r": project.repo, "then": then})
        linked = self.accounts.near_account(telegram_id)
        self.send(chat_id, (
            f"💰 <b>Fund the bounty treasury for {project.repo}</b>\n\n"
            + (f"Your linked wallet: <code>{linked}</code>\n\n" if linked else "")
            + 
            "Every fix is paid from this repository's treasury on NEAR, and only when you approve.\n\n"
            f"🔐 <b>Use my NEAR wallet</b>: deposit {WALLET_DEPOSIT_NEAR} testnet NEAR from your wallet. You own the "
            "treasury, and you'll approve each payout by signing with your wallet. "
            f"Free testnet NEAR: {FAUCET_URL}\n\n"
            f"🎁 <b>Let Mend fund it</b>: Mend deposits {self.settings.treasury_deposit_near} testnet NEAR for you "
            "and you approve with a tap here. No wallet needed."
        ), [[{"text": "🔐 Use my NEAR wallet", "url": link}],
            [{"text": "🎁 Let Mend fund it (testnet)", "callback_data": f"fund:{choice}"}]])

    def fund_with_mend(self, choice: str, telegram_id: str, notify: Notify) -> None:
        pending = self._fund_choices.get(choice)
        if pending is None or pending["telegram_id"] != telegram_id:
            raise ServiceError("This choice has expired. Send /try or /watch again.")
        project = self.settings.project_by_repo(pending["repo"])
        if project is None:
            raise ServiceError("That repository is no longer connected.")

        def work() -> None:
            from mend.integrations.escrow import register_repo_treasury
            notify(f"Opening a {self.settings.treasury_deposit_near} NEAR treasury for <b>{project.repo}</b>…")
            treasury = register_repo_treasury(self.settings.near_keys_file, project.repo, log=lambda l: None,
                                              deposit_near=self.settings.treasury_deposit_near,
                                              max_bounty_near=self.settings.max_bounty_near)
            self._fund_choices.pop(choice, None)
            escrow = self.orchestrator.escrow
            if escrow is not None and project.repo not in escrow.repos:
                escrow.repos.append(project.repo)
            notify(f"✅ Treasury open: {yocto_to_near(treasury.get('balance', 0))} NEAR, funded by Mend.")
            self._continue(telegram_id, project, pending["then"])

        self.run_async(telegram_id, "funding", work, notify)

    def wallet_funded(self, telegram_id: str, repo: str, then: str, account_id: str) -> Dict[str, Any]:
        """Called by the wallet page. Trusts nothing it is told: reads the treasury from the chain."""
        escrow = self.orchestrator.escrow
        project = self.settings.project_by_repo(repo)
        if escrow is None or project is None or project.owner_telegram_id != telegram_id:
            raise ServiceError("This link does not match a repository you connected.")
        on_chain = escrow.get_repo(repo)
        if not on_chain:
            raise ServiceError("The treasury is not on-chain yet. Wait a few seconds and try again.")
        if on_chain["owner"] != account_id:
            raise ServiceError(f"The treasury for {repo} is owned by {on_chain['owner']}, not {account_id}.")
        if on_chain.get("operator") != escrow.account_id("operator") or not on_chain.get("guard_agent"):
            raise ServiceError("The treasury exists but does not allow Mend's agents. Re-open the wallet link.")
        if int(on_chain["balance"]) < near_to_yocto(self.settings.bounty_near):
            raise ServiceError("The treasury has less than one bounty. Top it up from your wallet.")
        first_time = project.near_owner != account_id or bool(then)
        project.near_owner = account_id
        self._save(project)
        self.accounts.set_near_account(telegram_id, account_id)
        if repo not in escrow.repos:
            escrow.repos.append(repo)
        for key, value in list(self._fund_choices.items()):
            if value["repo"] == repo:
                self._fund_choices.pop(key, None)
        if first_time:
            user = self.accounts.get(telegram_id)
            chat_id = user.chat_id if user else telegram_id
            self.send(chat_id, (f"✅ Treasury for <b>{repo}</b> funded from your wallet <code>{account_id}</code>: "
                                f"{yocto_to_near(on_chain['balance'])} NEAR. You will approve payouts with this wallet."))
            notify = lambda text: self.send(chat_id, text)
            self.run_async(telegram_id, "continue", lambda: self._continue(telegram_id, project, then), notify)
        return {"repo": repo, "owner": account_id, "balance": on_chain["balance"]}

    def wallet_linked(self, telegram_id: str, account_id: str) -> Dict[str, Any]:
        """Links a NEAR account for display (its balance is public). Ownership is proven later, by signing."""
        escrow = self.orchestrator.escrow
        account = escrow.rpc.view_account(account_id) if escrow is not None else None
        if escrow is not None and account is None:
            raise ServiceError(f"{account_id} does not exist on NEAR {escrow.network}.")
        self.accounts.set_near_account(telegram_id, account_id)
        user = self.accounts.get(telegram_id)
        balance = yocto_to_near(account["amount"]) if account else "?"
        low = account is not None and int(account["amount"]) < near_to_yocto("0.3")
        self.send(user.chat_id if user else telegram_id, (
            f"✅ NEAR wallet linked: <code>{account_id}</code>\nBalance: <b>{balance} NEAR</b> ({escrow.network if escrow else 'testnet'})\n\n"
            + (f"That's not enough to fund a treasury (0.2 NEAR + fees). Get free testnet NEAR: {FAUCET_URL}\n\n" if low else "")
            + "When you fund a repository's treasury, this wallet signs it, and it signs each payout too. "
              "Next: /try"
        ))
        return {"account_id": account_id, "balance": account["amount"] if account else "0"}

    def _continue(self, telegram_id: str, project: Project, then: str) -> None:
        user = self.accounts.get(telegram_id)
        chat_id = user.chat_id if user else telegram_id
        notify = lambda text: self.send(chat_id, text)
        if then == "try":
            self.crash_demo(telegram_id, project, notify)

    def _save(self, project: Project) -> None:
        from mend.connect import save_connected
        save_connected(self.settings, project)

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
            self.offer_funding(telegram_id, result["project"], then="watch")

        self.run_async(telegram_id, "/watch", work, notify)

    # -- status ------------------------------------------------------------

    def status_lines(self, telegram_id: str) -> List[str]:
        user = self.accounts.get(telegram_id)
        lines = [f"GitHub: <b>{user.github_login}</b>" if user and user.github_login else "GitHub: not connected (/connect)"]
        escrow = self.orchestrator.escrow
        wallet = self.accounts.near_account(telegram_id)
        if wallet and escrow is not None:
            try:
                account = escrow.rpc.view_account(wallet)
                balance = yocto_to_near(account["amount"]) if account else "account not found"
            except NearError:
                balance = "unavailable right now"
            lines.append(f"NEAR wallet: <code>{wallet}</code>, balance {balance} NEAR")
        else:
            lines.append("NEAR wallet: none linked (choose “Use my NEAR wallet” when funding a treasury)")
        for project in self.projects_of(telegram_id):
            line = f"• <b>{project.repo}</b>"
            if escrow is not None and project.repo in escrow.repos:
                try:
                    repo = escrow.get_repo(project.repo) or {}
                    funded_by = f"your wallet {project.near_owner}" if project.near_owner else "Mend"
                    line += (f": {yocto_to_near(repo.get('balance', 0))} NEAR free, "
                             f"{yocto_to_near(repo.get('total_paid', 0))} paid to agents, funded by {funded_by}")
                except NearError:
                    line += ": treasury unreachable right now"
            lines.append(line)
        if len(lines) == 1:
            lines.append("No repositories yet. Send /try or /watch owner/repo.")
        return lines
