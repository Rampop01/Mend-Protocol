"""
Mend — Live Production HTTP Server, Switch AI Room API & Telegram Polling Daemon.
"""
from __future__ import annotations

import importlib
import json
import mimetypes
import os
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

from services import checkout_engine
from switch_room.orchestrator import (
    MendSwitchOrchestrator,
    SERVICE_FILE,
    SSL_CTX,
    load_env,
    save_env_key,
)

ROOT_DIR = Path(__file__).resolve().parent
WEB_DIR = ROOT_DIR / "web"

orchestrator = MendSwitchOrchestrator()


def telegram_polling_loop() -> None:
    """
    Background daemon thread that long-polls Telegram Bot API (`@mend_switch_bot`)
    for real-time human messages, @agent commands, and inline button approvals.
    """
    offset = 0
    while True:
        try:
            env = load_env()
            token = env.get("TELEGRAM_BOT_TOKEN")
            if not token:
                time.sleep(3)
                continue

            url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=15&offset={offset}"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as resp:
                data = json.loads(resp.read().decode())

            for upd in data.get("result", []):
                offset = max(offset, int(upd["update_id"]) + 1)

                # Handle inline keyboard button click (Approve / Reject)
                if "callback_query" in upd:
                    cb = upd["callback_query"]
                    cb_id = cb["id"]
                    cb_data = cb.get("data", "")
                    from_user = cb.get("from", {}).get("first_name", "Engineer")
                    chat_id = cb.get("message", {}).get("chat", {}).get("id")
                    if chat_id:
                        orchestrator.telegram_chat_id = int(chat_id)
                        save_env_key("TELEGRAM_CHAT_ID", str(chat_id))

                    # Acknowledge button tap on Telegram
                    ack_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                    ack_payload = json.dumps({
                        "callback_query_id": cb_id,
                        "text": "Mend Swarm processing on NEAR...",
                    }).encode("utf-8")
                    urllib.request.urlopen(
                        urllib.request.Request(
                            ack_url,
                            data=ack_payload,
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        ),
                        timeout=8,
                        context=SSL_CTX,
                    )

                    if cb_data.startswith("approve:"):
                        orchestrator.approve_and_deploy_patch(approver=f"{from_user} (Telegram Button)")
                    elif cb_data.startswith("reject:"):
                        orchestrator.reset_to_unpatched(actor=f"{from_user} (Telegram Button)")
                    continue

                # Handle standard or group chat message
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue

                chat_id = msg.get("chat", {}).get("id")
                from_user = msg.get("from", {}).get("first_name", "Engineer")
                text = msg.get("text", "")

                if chat_id:
                    orchestrator.telegram_chat_id = int(chat_id)
                    save_env_key("TELEGRAM_CHAT_ID", str(chat_id))

                if text:
                    orchestrator.handle_incoming_chat_command(
                        sender_name=from_user,
                        text=text,
                        platform="Telegram",
                    )

        except Exception:
            time.sleep(2)


class MendRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status_code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            source_code = SERVICE_FILE.read_text()
            is_patched = "max(0, gross_cents - discount_cents)" in source_code
            env = load_env()
            chat_id = orchestrator.telegram_chat_id or env.get("TELEGRAM_CHAT_ID")
            self._send_json(
                200,
                {
                    "service_status": "HEALTHY_PATCHED" if is_patched else "UNPATCHED_VULNERABLE",
                    "is_patched": is_patched,
                    "telegram": {
                        "connected": bool(env.get("TELEGRAM_BOT_TOKEN")),
                        "bot_username": orchestrator.telegram_bot_username,
                        "chat_id": chat_id,
                    },
                    "active_incident": orchestrator.active_incident,
                    "pending_diff": orchestrator.pending_diff,
                    "last_test_report": orchestrator.last_test_report,
                    "last_guardrail_report": orchestrator.last_guardrail_report,
                    "source_code": source_code,
                    "settlements": checkout_engine.list_recent_settlements(limit=10),
                    "messages": orchestrator.get_room_messages(limit=50),
                },
            )
            return

        # Serve static files from web/
        rel_path = parsed.path.lstrip("/") or "index.html"
        target_file = (WEB_DIR / rel_path).resolve()
        if not str(target_file).startswith(str(WEB_DIR.resolve())) or not target_file.is_file():
            target_file = WEB_DIR / "index.html"

        content = target_file.read_bytes()
        mime, _ = mimetypes.guess_type(str(target_file))
        self.send_response(200)
        self.send_header("Content-Type", f"{mime or 'text/html'}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        body = self._read_json()

        if parsed.path == "/api/checkout/settle":
            try:
                importlib.reload(checkout_engine)
                result = checkout_engine.process_settlement(body)
                self._send_json(200, {"ok": True, "settlement": result.to_dict()})
            except Exception as exc:
                tb_str = traceback.format_exc()
                incident_bundle = orchestrator.handle_production_exception(body, exc)
                self._send_json(
                    500,
                    {
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "traceback": tb_str,
                        "incident_bundle": incident_bundle,
                    },
                )
            return

        if parsed.path == "/api/room/approve":
            approver = str(body.get("approver", "Lead Engineer (Switch Console)"))
            res = orchestrator.approve_and_deploy_patch(approver=approver)
            self._send_json(200, {"ok": True, "result": res})
            return

        if parsed.path == "/api/room/reset":
            actor = str(body.get("actor", "Lead Engineer"))
            res = orchestrator.reset_to_unpatched(actor=actor)
            self._send_json(200, {"ok": True, "result": res})
            return

        if parsed.path == "/api/room/test":
            report = orchestrator.run_real_unittest_suite(candidate_source=None)
            orchestrator.post_room_message(
                sender_id="@mend-forge",
                sender_name="mend-forge",
                sender_role="agent-forge",
                platform="Switch Protocol // Telegram",
                content=(
                    f"ON-DEMAND UNITTEST SUITE: {'PASSED' if report['passed'] else 'FAILED'} "
                    f"(exit={report['exit_code']}) in {report['elapsed_ms']}ms"
                ),
                metadata={"test_report": report},
            )
            self._send_json(200, {"ok": True, "report": report})
            return

        if parsed.path == "/api/room/message":
            sender = str(body.get("sender", "Rahmat (Engineer)"))
            text = str(body.get("text", "")).strip()
            platform = str(body.get("platform", "Switch Room Console"))
            if not text:
                self._send_json(400, {"ok": False, "error": "Empty message"})
                return
            res = orchestrator.handle_incoming_chat_command(sender_name=sender, text=text, platform=platform)
            self._send_json(200, {"ok": True, "result": res})
            return

        self._send_json(404, {"ok": False, "error": "Not found"})


def run_server(port: int = 8770) -> None:
    checkout_engine.init_db()
    poller = threading.Thread(target=telegram_polling_loop, daemon=True)
    poller.start()
    server = ThreadingHTTPServer(("127.0.0.1", port), MendRequestHandler)
    print(f"Mend Protocol // NEAR Swarm Engine listening on http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    import importlib
    port = int(os.environ.get("PORT", "8770"))
    run_server(port=port)
