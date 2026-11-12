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
    def __init__(self, token: str, chat_id: str, approver_ids: List[str], orchestrator: SwarmOrchestrator) -> None:
        self.token = token
        self.chat_id = str(chat_id).strip()
        self.approver_ids = {str(a) for a in approver_ids}
        self.orchestrator = orchestrator
        self.running = False
        self._offset = 0
        self._thread: Optional[threading.Thread] = None
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

    def edit(self, message_id: int, text: str, keyboard: Optional[List[List[Dict[str, str]]]] = None) -> None:
        data: Dict[str, Any] = {
            "chat_id": self.chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True, "reply_markup": {"inline_keyboard": keyboard or []},
        }
        result = self.api("editMessageText", data)
        if not result.get("ok") and "not modified" not in str(result.get("description")):
            logger.warning("Telegram editMessageText failed: %s", result.get("description"))

    # ------------------------------------------------------------------
    # Incident cards
    # ------------------------------------------------------------------

    def on_incident(self, incident: Dict[str, Any], event: str) -> None:
        if not self.chat_id:
            return
        text, keyboard = self.render(incident)
        message_id = (incident.get("telegram") or {}).get("message_id")
        if message_id is None:
            message_id = self.send(self.chat_id, text, keyboard)
            if message_id is not None:
                incident["telegram"] = {"message_id": message_id}
        else:
            self.edit(message_id, text, keyboard)

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
            keyboard = [
                [{"text": label, "callback_data": f"approve:{incident['incident_id']}"}],
                [{"text": "❌ Reject", "callback_data": f"reject:{incident['incident_id']}"}],
            ]
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

    def _authorized(self, chat_id: Any, user_id: Any) -> bool:
        if not self.chat_id or str(chat_id) != self.chat_id:
            return False
        return not self.approver_ids or str(user_id) in self.approver_ids

    def handle_callback(self, query: Dict[str, Any]) -> None:
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        user = query.get("from") or {}
        action, _, incident_id = str(query.get("data") or "").partition(":")
        if action not in ("approve", "reject") or not incident_id:
            self.api("answerCallbackQuery", {"callback_query_id": query.get("id")})
            return
        if not self._authorized(chat_id, user.get("id")):
            self.api("answerCallbackQuery", {
                "callback_query_id": query.get("id"), "text": "You are not an approver for this Mend server.",
                "show_alert": True,
            })
            logger.warning("Refused %s of %s by Telegram user %s", action, incident_id, user.get("id"))
            return
        who = f"@{user['username']}" if user.get("username") else f"telegram:{user.get('id')}"
        self.api("answerCallbackQuery", {
            "callback_query_id": query.get("id"),
            "text": "Approving: settling on NEAR…" if action == "approve" else "Rejecting…",
        })

        def run() -> None:
            try:
                if action == "approve":
                    self.orchestrator.approve(incident_id, approver=who)
                else:
                    self.orchestrator.reject(incident_id, rejector=who)
            except IncidentError as exc:
                self.send(self.chat_id, f"⚠️ {esc(exc)}")
            except Exception:
                logger.exception("Telegram %s failed", action)
                self.send(self.chat_id, f"⚠️ {action.title()} of <code>{esc(incident_id)}</code> failed. See the server log.")

        threading.Thread(target=run, daemon=True).start()

    def handle_message(self, message: Dict[str, Any]) -> None:
        chat_id = (message.get("chat") or {}).get("id")
        user_id = (message.get("from") or {}).get("id")
        command = str(message.get("text") or "").split()[0].split("@")[0] if message.get("text") else ""

        if command in ("/start", "/whoami"):
            # Answered in any chat: this is how you find the ids to put in .env.
            self.send(chat_id, (
                "<b>Mend Protocol</b>\n"
                f"This chat id: <code>{chat_id}</code>\nYour user id: <code>{user_id}</code>\n\n"
                "Set <code>TELEGRAM_CHAT_ID</code> to the chat that should receive incidents and "
                "<code>TELEGRAM_APPROVER_IDS</code> to the users allowed to approve.\n\n"
                "/status  treasury, agents and network\n/incidents  recent incidents"
            ))
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
