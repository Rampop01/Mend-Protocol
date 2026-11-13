"""
Telegram control plane.

One message per incident, edited in place as the swarm works: detected, then
the reviewed patch with Approve / Reject buttons, then the settlement with
links to the real transactions.

Only the configured chat is served, and only listed approvers may approve or
reject when TELEGRAM_APPROVER_IDS is set.
"""
from __future__ import annotations

import html
import json
import re
import logging
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from mend.integrations.near import NearError, yocto_to_near
from mend.swarm.orchestrator import IncidentError, SwarmOrchestrator

logger = logging.getLogger("mend.bot")

MAX_DIFF_CHARS = 1800


def _ssl_context() -> ssl.SSLContext:
    cafile = "/etc/ssl/cert.pem"
    return ssl.create_default_context(cafile=cafile) if Path(cafile).exists() else ssl.create_default_context()


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


class TelegramBot:
    def __init__(self, token: str, chat_id: str, approver_ids: List[str], orchestrator: SwarmOrchestrator,
                 hosted: Optional[Any] = None) -> None:
        self.token = token
        self.chat_id = str(chat_id).strip()
        self.approver_ids = {str(a) for a in approver_ids}
        self.orchestrator = orchestrator
        self.running = False
        self._offset = 0
        self._thread: Optional[threading.Thread] = None
        self._in_flight: set = set()
        self._awaiting_repo: Dict[str, bool] = {}  # users asked "which repository?" by /watch
        self._in_flight_lock = threading.Lock()
        # On a public service, anyone may use the bot; each incident belongs to one user.
        self.hosted = hosted
        if hosted is not None:
            hosted.send = lambda chat_id, text, keyboard=None: self.send(chat_id, text, keyboard)
        orchestrator.listeners.append(self.on_incident)

    # ------------------------------------------------------------------
    # Telegram API
    # ------------------------------------------------------------------

    def api(self, method: str, data: Optional[Dict[str, Any]] = None, timeout: float = 20.0) -> Dict[str, Any]:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(data or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read().decode("utf-8"))
            except ValueError:
                return {"ok": False, "description": f"HTTP {exc.code}"}
        except Exception as exc:
            # Never log the URL: it contains the bot token.
            return {"ok": False, "description": type(exc).__name__}

    def send(self, chat_id: Any, text: str, keyboard: Optional[List[List[Dict[str, str]]]] = None) -> Optional[int]:
        data: Dict[str, Any] = {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
        }
        if keyboard is not None:
            data["reply_markup"] = {"inline_keyboard": keyboard}
        result = self.api("sendMessage", data)
        if not result.get("ok"):
            logger.warning("Telegram sendMessage failed: %s", result.get("description"))
            return None
        return result["result"]["message_id"]

    def edit(self, message_id: int, text: str, keyboard: Optional[List[List[Dict[str, str]]]] = None,
             chat_id: Optional[str] = None) -> None:
        data: Dict[str, Any] = {
            "chat_id": chat_id or self.chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True, "reply_markup": {"inline_keyboard": keyboard or []},
        }
        result = self.api("editMessageText", data)
        if not result.get("ok") and "not modified" not in str(result.get("description")):
            logger.warning("Telegram editMessageText failed: %s", result.get("description"))

    # ------------------------------------------------------------------
    # Incident cards
    # ------------------------------------------------------------------

    def on_incident(self, incident: Dict[str, Any], event: str) -> None:
        chat_id = incident.get("chat_id") or self.chat_id
        if not chat_id:
            return
        text, keyboard = self.render(incident)
        message_id = (incident.get("telegram") or {}).get("message_id")
        if message_id is None:
            message_id = self.send(chat_id, text, keyboard)
            if message_id is not None:
                incident["telegram"] = {"message_id": message_id, "chat_id": str(chat_id)}
        else:
            self.edit(message_id, text, keyboard, chat_id=(incident.get("telegram") or {}).get("chat_id") or chat_id)

    def render(self, incident: Dict[str, Any]) -> Any:
        status = incident["status"]
        head = (
            f"<b>{esc(incident['error_type'])}</b>: <code>{esc(incident['error_message'][:200])}</code>\n"
            f"<code>{esc(incident['rel_path'])}:{incident['line_number']}</code> in <code>{esc(incident['repo'])}</code>\n"
            f"Incident <code>{incident['incident_id']}</code>"
        )
        if incident.get("occurrences", 1) > 1:
            head += f" (seen {incident['occurrences']} times)"
        keyboard: List[List[Dict[str, str]]] = []

        if status in ("RECEIVED", "ANALYZING"):
            steps = "\n".join(f"• <b>{esc(t['agent'])}</b>: {esc(t['message'])}" for t in incident["timeline"][-6:])
            return f"🚨 <b>Production crash detected</b>\n\n{head}\n\n<i>The swarm is working…</i>\n{steps}", keyboard

        if status == "STALE":
            return f"ℹ️ <b>Crash in an older deployment</b>\n\n{head}\n\n{esc(incident['timeline'][-1]['message'])}", keyboard

        if status in ("FAILED", "NO_FIX"):
            reason = incident.get("error") or "No candidate patch passed the guard and the test suite."
            lines = [f"⚠️ <b>No automatic fix</b>\n\n{head}\n\n{esc(reason)}"]
            lines += self._candidate_lines(incident)
            return "\n".join(lines), keyboard

        winner = next((c for c in incident["candidates"] if c["strategy"] == incident.get("winner")), None)
        if winner is None:
            return f"{head}\n\nStatus: {esc(status)}", keyboard
        guard, run = winner["guard"], winner["forge_run"]
        baseline = (incident.get("recon") or {}).get("baseline") or {}
        evidence = [f"• Tests with the patch: <b>{run['tests_run'] - run['tests_failed']}/{run['tests_run']} passed</b>"]
        if baseline.get("tests_failed"):
            evidence.append(f"• Before the patch: {baseline['tests_failed']} of {baseline['tests_run']} failed")
        evidence.append(f"• Guard score: <b>{guard['score']}/100</b> (independent re-run)")
        diff = winner["diff"]
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n… (truncated; full diff in the dashboard)"
        body = (
            f"<b>Proposed fix</b> by <code>forge-{esc(winner['strategy'])}</code>: {esc(winner['rationale'])}\n"
            + "\n".join(evidence)
            + "\n" + "\n".join(self._candidate_lines(incident))
            + f"\n\n<pre><code class=\"language-diff\">{esc(diff)}</code></pre>"
        )
        chain = incident["chain"]

        if status in ("AWAITING_APPROVAL", "APPROVING"):
            escrow = (
                f"💰 {esc(incident['bounty_near'])} NEAR reserved in <code>{esc(chain['contract_id'])}</code>"
                if chain["enabled"] else f"ℹ️ No on-chain escrow: {esc(chain.get('error') or 'not configured')}"
            )
            label = (f"✅ Approve and release {incident['bounty_near']} NEAR" if chain["enabled"] else "✅ Approve fix")
            approve: Dict[str, str] = {"text": label, "callback_data": f"approve:{incident['incident_id']}"}
            if incident.get("near_owner") and chain["enabled"] and self.hosted is not None:
                # The treasury is the user's: the payout must be signed by their wallet, not by Mend.
                link = self.hosted.wallet_link({"k": "approve", "u": incident.get("owner_telegram_id", ""),
                                                "i": incident["incident_id"]}, ttl=86400)
                approve = {"text": f"✅ Approve with NEAR wallet ({incident['bounty_near']} NEAR)", "url": link}
                escrow += f"\n🔐 Your wallet <code>{esc(incident['near_owner'])}</code> signs the payout."
            keyboard = [[approve], [{"text": "❌ Reject", "callback_data": f"reject:{incident['incident_id']}"}]]
            return f"🛠 <b>Fix ready for review</b>\n\n{head}\n\n{body}\n\n{escrow}", keyboard

        if status == "REJECTED":
            note = "Bounty returned to the treasury." if chain["enabled"] else ""
            return f"❌ <b>Fix rejected</b>\n\n{head}\n\n{note}", keyboard

        # RESOLVED
        resolution = incident.get("resolution") or {}
        lines = [f"✅ <b>Fix approved by {esc(resolution.get('approver', '?'))}</b>\n\n{head}\n\n{body}\n"]
        if resolution.get("settlement"):
            lines.append("<b>Settled on NEAR</b>")
            lines += [
                f"• {esc(p['role'])}: <b>{esc(p['amount_near'])} NEAR</b> → <code>{esc(p['account'])}</code>"
                for p in resolution.get("payouts", [])
            ]
            keyboard.append([{"text": "View settlement transaction", "url": resolution["settlement"]["explorer_url"]}])
        if resolution.get("pr_url"):
            keyboard.append([{"text": "View pull request", "url": resolution["pr_url"]}])
        lines += [f"ℹ️ {esc(n)}" for n in resolution.get("notes", [])]
        return "\n".join(lines), keyboard

    @staticmethod
    def _candidate_lines(incident: Dict[str, Any]) -> List[str]:
        lines = []
        for c in incident.get("candidates", []):
            if c.get("declined"):
                lines.append(f"• forge-{esc(c['strategy'])}: no proposal ({esc(c['declined'][:120])})")
            elif c["strategy"] != incident.get("winner") and c.get("guard"):
                verdict = "passed" if c["guard"]["passed"] and c["guard"]["executed"] else "rejected by guard"
                lines.append(f"• forge-{esc(c['strategy'])}: {verdict}, score {c['guard']['score']}/100")
        return lines

    # ------------------------------------------------------------------
    # Updates
    # ------------------------------------------------------------------

    def _authorized(self, chat_id: Any, user_id: Any, incident: Optional[Dict[str, Any]] = None) -> bool:
        owner = (incident or {}).get("owner_telegram_id")
        if owner:
            # A user's incident can only be decided by that user, in their private chat with the bot.
            return str(user_id) == str(owner) and str(chat_id) == str(user_id)
        if not self.chat_id or str(chat_id) != self.chat_id:
            return False
        return not self.approver_ids or str(user_id) in self.approver_ids

    def handle_callback(self, query: Dict[str, Any]) -> None:
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        user = query.get("from") or {}
        action, _, incident_id = str(query.get("data") or "").partition(":")
        if action == "cmd" and self.hosted is not None:
            self.api("answerCallbackQuery", {"callback_query_id": query.get("id")})
            self._public_command("/" + incident_id, "", chat_id, user)
            return
        if action == "fund" and self.hosted is not None:
            self.api("answerCallbackQuery", {"callback_query_id": query.get("id"), "text": "Opening your treasury…"})
            if message.get("message_id"):
                self.api("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message["message_id"],
                                                    "reply_markup": {"inline_keyboard": []}})
            try:
                self.hosted.fund_with_mend(incident_id, str(user.get("id")), lambda text: self.send(chat_id, text))
            except Exception as exc:
                self.send(chat_id, f"⚠️ {esc(exc)}")
            return
        if action not in ("approve", "reject") or not incident_id:
            self.api("answerCallbackQuery", {"callback_query_id": query.get("id")})
            return
        if not self._authorized(chat_id, user.get("id"), self.orchestrator.store.get(incident_id)):
            self.api("answerCallbackQuery", {
                "callback_query_id": query.get("id"), "text": "You are not an approver for this Mend server.",
                "show_alert": True,
            })
            logger.warning("Refused %s of %s by Telegram user %s", action, incident_id, user.get("id"))
            return
        who = f"@{user['username']}" if user.get("username") else f"telegram:{user.get('id')}"

        # A second tap, or a tap on an old card, is answered quietly instead of posting an error.
        incident = self.orchestrator.store.get(incident_id)
        with self._in_flight_lock:
            busy = incident_id in self._in_flight
            if not busy and incident is not None and incident["status"] == "AWAITING_APPROVAL":
                self._in_flight.add(incident_id)
                busy = None  # this tap proceeds
        if busy is not None:
            state = "already being handled" if busy else f"already {incident['status'].lower().replace('_', ' ')}" if incident else "unknown"
            self.api("answerCallbackQuery", {"callback_query_id": query.get("id"), "text": f"This incident is {state}."})
            return

        self.api("answerCallbackQuery", {
            "callback_query_id": query.get("id"),
            "text": "Approving: settling on NEAR…" if action == "approve" else "Rejecting…",
        })
        # Remove the buttons at once so nobody can tap twice.
        if message.get("message_id"):
            self.api("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message["message_id"],
                                                "reply_markup": {"inline_keyboard": []}})

        def run() -> None:
            try:
                if action == "approve":
                    self.orchestrator.approve(incident_id, approver=who)
                else:
                    self.orchestrator.reject(incident_id, rejector=who)
            except IncidentError as exc:
                self.send(chat_id, f"⚠️ {esc(exc)}")
                current = self.orchestrator.store.get(incident_id)
                if current is not None:
                    self.on_incident(current, "retry")  # puts the buttons back if it can be retried
                    self.orchestrator.store.save(current)
            except Exception:
                logger.exception("Telegram %s failed", action)
                self.send(chat_id, f"⚠️ {action.title()} of <code>{esc(incident_id)}</code> failed. See the server log.")
            finally:
                with self._in_flight_lock:
                    self._in_flight.discard(incident_id)

        threading.Thread(target=run, daemon=True).start()

    def handle_message(self, message: Dict[str, Any]) -> None:
        chat_id = (message.get("chat") or {}).get("id")
        user_id = (message.get("from") or {}).get("id")
        command = str(message.get("text") or "").split()[0].split("@")[0] if message.get("text") else ""
        if self.hosted is not None:
            if (message.get("chat") or {}).get("type") != "private":
                return  # the public bot works in private chats only
            args = str(message.get("text") or "").split(maxsplit=1)
            self._public_command(command, args[1] if len(args) > 1 else "", chat_id, message.get("from") or {})
            return

        if command in ("/start", "/whoami"):
            # Answered in any chat: this is how you find the ids to put in .env.
            # remove_keyboard clears reply keyboards left behind by older bot versions.
            self.api("sendMessage", {"chat_id": chat_id, "parse_mode": "HTML", "reply_markup": {"remove_keyboard": True}, "text": (
                "<b>Mend Protocol</b>\n"
                f"This chat id: <code>{chat_id}</code>\nYour user id: <code>{user_id}</code>\n\n"
                "Set <code>TELEGRAM_CHAT_ID</code> to the chat that should receive incidents and "
                "<code>TELEGRAM_APPROVER_IDS</code> to the users allowed to approve.\n\n"
                "/status  treasury, agents and network\n/incidents  recent incidents"
            )})
            return
        if str(chat_id) != self.chat_id:
            return
        if command == "/status":
            self.send(chat_id, self._status_text())
        elif command == "/incidents":
            recent = self.orchestrator.store.list(limit=8)
            if not recent:
                self.send(chat_id, "No incidents yet.")
                return
            self.send(chat_id, "\n".join(
                f"<code>{i['incident_id']}</code> {esc(i['status'])}: {esc(i['error_type'])} at "
                f"<code>{esc(i['rel_path'])}:{i['line_number']}</code>" for i in recent
            ))

    # ------------------------------------------------------------------
    # Public service commands
    # ------------------------------------------------------------------

    WELCOME = (
        "<b>Mend</b> fixes production crashes with a team of AI agents, and pays them on <b>NEAR</b> "
        "only when you approve.\n\n"
        "🔍 <b>Recon</b> finds the broken line\n"
        "🔧🤖 Two <b>Forge</b> agents compete to fix it (rules vs. AI)\n"
        "🛡️ <b>Guard</b> re-runs every test and blocks unsafe code\n"
        "📱 <b>You</b> approve here with one tap; the fix arrives as a GitHub pull request\n\n"
        "<b>Try it in two minutes:</b>\n"
        "1. /connect: sign in to GitHub (public repos only)\n"
        "2. /wallet: connect your NEAR wallet (optional) to own your bounty treasury and sign payouts\n"
        "3. /try: Mend forks a demo shop into your GitHub, crashes its checkout, and fixes it\n\n"
        "No NEAR wallet? Skip step 2 and Mend funds your treasury with testnet NEAR.\n"
        "Then /watch owner/repo to protect your own Python project.\n"
        "/status · /disconnect · /help\n\n"
        "<i>Bounties use NEAR testnet tokens. Open source: github.com/Rampop01/Mend-Protocol</i>"
    )

    # Buttons from an older version of the bot that some chats still show.
    LEGACY_BUTTONS = {"Connect GitHub": "/connect", "Trigger Incident": "/try", "NEAR Treasury": "/status",
                      "Run Test Suite": "/help", "Ship Hotfix": "/help", "Reset Baseline": "/help"}

    REPO_IN_TEXT = re.compile(r"github\.com[/:]([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})", re.IGNORECASE)
    BARE_REPO = re.compile(r"^\s*([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})\s*$")

    @classmethod
    def repo_from_text(cls, text: str) -> Optional[str]:
        """Finds a GitHub repository in free text: a github.com link anywhere, or a bare owner/repo."""
        match = cls.REPO_IN_TEXT.search(text) or cls.BARE_REPO.match(text)
        if not match:
            return None
        name = match.group(2)
        name = name[:-4] if name.lower().endswith(".git") else name
        return f"{match.group(1)}/{name.rstrip('.')}"

    def _clear_keyboard(self, chat_id: Any) -> None:
        """Removes a reply keyboard left by an older bot version. The carrier message is deleted right away."""
        sent = self.api("sendMessage", {"chat_id": chat_id, "text": "Menu updated.",
                                        "reply_markup": {"remove_keyboard": True}})
        if sent.get("ok"):
            self.api("deleteMessage", {"chat_id": chat_id, "message_id": sent["result"]["message_id"]})

    def _public_command(self, command: str, arg: str, chat_id: Any, user: Dict[str, Any]) -> None:
        hosted = self.hosted
        user_id = str(user.get("id"))
        hosted.accounts.upsert(user_id, str(chat_id), user.get("username") or "")
        notify = lambda text: self.send(chat_id, text)
        if not command.startswith("/"):
            text = " ".join(f"{command} {arg}".split()).strip()
            legacy = next((cmd for name, cmd in self.LEGACY_BUTTONS.items() if text.endswith(name)), None)
            repo = self.repo_from_text(text)
            if legacy:
                self._clear_keyboard(chat_id)
                command, arg = legacy, ""
            elif repo and (self._awaiting_repo.pop(user_id, False) or "github.com" in text.lower()):
                command, arg = "/watch", repo  # the answer to "which repository?", or a pasted GitHub link
            elif self._awaiting_repo.pop(user_id, False):
                notify("That doesn't look like a GitHub repository. Send it as <code>owner/repo</code> or paste its "
                       "github.com link, or send /watch to start again.")
                return
            else:
                notify("I can protect a GitHub repository: paste its link, or send <code>/watch owner/repo</code>. "
                       "Send /help to see everything I can do.")
                return
        try:
            if command in ("/start", "/help"):
                if command == "/start":
                    self._clear_keyboard(chat_id)
                self.api("sendMessage", {"chat_id": chat_id, "text": self.WELCOME, "parse_mode": "HTML",
                                         "disable_web_page_preview": True,
                                         "reply_markup": {"inline_keyboard": [
                                             [{"text": "1. Connect GitHub", "callback_data": "cmd:connect"}],
                                             [{"text": "2. Connect NEAR wallet (optional)",
                                               "url": hosted.wallet_link({"k": "link", "u": user_id}, ttl=86400)}],
                                             [{"text": "3. Try the demo", "callback_data": "cmd:try"}]]}})
            elif command == "/connect":
                account = hosted.accounts.get(user_id)
                if account and account.github_login and arg.strip() != "again":
                    notify(f"Already connected as <b>{esc(account.github_login)}</b>. Send /try, or /disconnect first.")
                    return

                def show_code(code: str, url: str) -> None:
                    self.send(chat_id, (
                        f"Open <b>{esc(url)}</b> and enter this code:\n\n<code>{esc(code)}</code>\n\n"
                        "Mend asks for access to your <b>public</b> repositories, so it can fork the demo and open "
                        "pull requests. I'll message you as soon as you approve."
                    ), keyboard=[[{"text": "Open GitHub", "url": url}]])

                hosted.connect_github(user_id, notify, show_code)
            elif command == "/wallet":
                linked = hosted.accounts.near_account(user_id)
                escrow = self.orchestrator.escrow
                text = "Connect a NEAR testnet wallet (Meteor, MyNearWallet or Intear). It will own your bounty " \
                       "treasuries and sign each payout."
                if linked and escrow is not None:
                    try:
                        account = escrow.rpc.view_account(linked)
                        balance = yocto_to_near(account["amount"]) if account else "account not found"
                    except Exception:
                        balance = "unavailable right now"
                    text = f"Linked wallet: <code>{esc(linked)}</code>\nBalance: <b>{balance} NEAR</b>\n\nOpen the page to switch wallets."
                self.send(chat_id, text, keyboard=[[{"text": "🔐 Connect NEAR wallet" if not linked else "🔐 Switch wallet",
                                                     "url": hosted.wallet_link({"k": "link", "u": user_id}, ttl=86400)}]])
            elif command == "/disconnect":
                hosted.accounts.clear_github(user_id)
                notify("GitHub disconnected; Mend deleted your token. "
                       "You can also revoke access at github.com/settings/applications.")
            elif command == "/try":
                hosted.try_demo(user_id, notify)
            elif command == "/watch":
                repo = self.repo_from_text(arg) if arg.strip() else None
                if not repo:
                    self._awaiting_repo[user_id] = True
                    notify("Which repository should Mend protect? Send <code>owner/repo</code> or paste its GitHub link.\n\n"
                           "It must be a <b>public Python</b> repository with tests (pytest or unittest) that you can push to.")
                    return
                hosted.watch(user_id, repo, notify)
            elif command in ("/repos", "/status"):
                notify("\n".join(hosted.status_lines(user_id)))
            elif command == "/whoami":
                notify(f"Your Telegram id: <code>{esc(user_id)}</code>")
            else:
                notify("I didn't recognise that. Send /help to see what I can do.")
        except Exception as exc:  # ServiceError and friends carry a user-facing message
            from mend.hosted import ServiceError
            from mend.accounts import AccountError
            if isinstance(exc, (ServiceError, AccountError)):
                notify(f"⚠️ {esc(exc)}")
            else:
                logger.exception("Command %s failed", command)
                notify("⚠️ Something went wrong on the Mend server. Please try again in a minute.")

    def _status_text(self) -> str:
        escrow = self.orchestrator.escrow
        counts = self.orchestrator.store.count()
        lines = ["<b>Mend status</b>", "Incidents: " + (", ".join(f"{n} {s.lower()}" for s, n in sorted(counts.items())) or "none")]
        lines.append("LLM forge agent: " + ("enabled" if self.orchestrator.llm_forge.available else "disabled (no API key)"))
        if escrow is None:
            lines.append("NEAR: not configured (run <code>mend setup</code>)")
            return "\n".join(lines)
        try:
            overview = escrow.overview()
        except NearError as exc:
            lines.append(f"NEAR: unreachable ({esc(exc)})")
            return "\n".join(lines)
        lines.append(f"\n<b>NEAR {esc(overview['network'])}</b>: <code>{esc(overview['contract_id'])}</code>")
        for repo_id, repo in overview["repos"].items():
            if repo:
                lines.append(
                    f"Treasury {esc(repo_id)}: {yocto_to_near(repo['balance'])} NEAR free, "
                    f"{yocto_to_near(repo['reserved'])} reserved, {yocto_to_near(repo['total_paid'])} paid"
                )
        for agent in overview["agents"]:
            lines.append(f"• {esc(agent['role'])}: earned {yocto_to_near(agent['earned'])} NEAR")
        return "\n".join(lines)

    def start(self) -> None:
        me = self.api("getMe")
        if not me.get("ok"):
            raise RuntimeError(f"Telegram rejected the bot token: {me.get('description')}")
        logger.info("Telegram bot @%s connected", me["result"].get("username"))
        if self.hosted is not None:
            resumed = self.hosted.resume_logins(lambda uid: (lambda text: self.send(uid, text)))
            if resumed:
                logger.info("Resumed %d GitHub sign-in(s) in progress", resumed)
        commands = [
            {"command": "status", "description": "Treasury, agents and network"},
            {"command": "incidents", "description": "Recent incidents"},
            {"command": "whoami", "description": "Show this chat's id and your user id"},
        ]
        if self.hosted is not None:
            commands = [
                {"command": "start", "description": "What Mend does"},
                {"command": "connect", "description": "Sign in to GitHub"},
                {"command": "wallet", "description": "Connect your NEAR wallet and see its balance"},
                {"command": "try", "description": "Watch Mend fix a real bug on your fork"},
                {"command": "watch", "description": "Protect one of your repositories"},
                {"command": "repos", "description": "Your repositories and NEAR treasuries"},
                {"command": "disconnect", "description": "Remove your GitHub access"},
            ]
        self.api("setMyCommands", {"commands": commands})
        self.running = True

        def loop() -> None:
            while self.running:
                result = self.api(
                    "getUpdates",
                    {"offset": self._offset + 1, "timeout": 25, "allowed_updates": ["message", "callback_query"]},
                    timeout=35.0,
                )
                if not result.get("ok"):
                    time.sleep(3)
                    continue
                for update in result.get("result", []):
                    self._offset = max(self._offset, update.get("update_id", 0))
                    try:
                        if "callback_query" in update:
                            self.handle_callback(update["callback_query"])
                        elif "message" in update:
                            self.handle_message(update["message"])
                    except Exception:
                        logger.exception("Failed to handle Telegram update")

        self._thread = threading.Thread(target=loop, name="mend-telegram", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
