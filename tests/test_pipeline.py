"""End-to-end tests of the swarm pipeline and the HTTP server against a real temporary project."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from mend.server import MendServer
from mend.swarm.orchestrator import IncidentError, SwarmOrchestrator
from tests.helpers import (APP, RecordingEscrow, ScriptedLLM, crash_payload, make_project, make_settings, temp_dir)

ZERO_DIV = ("amount / rate", "ZeroDivisionError", "float division by zero")


class PipelineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir()
        self.addCleanup(self.tmp.cleanup)  # registered first, so it runs after every orchestrator has stopped
        self.root = Path(self.tmp.name)
        self.project = make_project(self.root)
        self.settings = make_settings(self.root, self.project)

    def orchestrator(self, escrow=None, llm=None):
        orch = SwarmOrchestrator(self.settings, escrow=escrow, llm=llm)
        orch.start()
        self.addCleanup(orch.stop)
        return orch

    def run_incident(self, orch, crash=ZERO_DIV, **kwargs):
        incident = orch.report(crash_payload(self.project, *crash, **kwargs))
        orch.wait_idle()
        return orch.store.get(incident["incident_id"])


class PipelineTests(PipelineCase):
    def test_crash_is_fixed_verified_and_applied_only_after_approval(self):
        orch = self.orchestrator()
        incident = self.run_incident(orch)
        self.assertEqual(incident["status"], "AWAITING_APPROVAL")
        self.assertEqual(incident["winner"], "rules")
        self.assertEqual(incident["recon"]["baseline"]["tests_failed"], 1)
        winner = incident["candidates"][0]
        self.assertEqual((winner["forge_run"]["tests_run"], winner["forge_run"]["tests_failed"]), (3, 0))
        self.assertTrue(winner["guard"]["passed"])
        self.assertEqual(winner["guard"]["score"], 100)
        # Nothing touches the working tree before a human approves.
        self.assertEqual((self.project.root / "app.py").read_text(), APP)

        resolved = orch.approve(incident["incident_id"], approver="tester")
        self.assertEqual(resolved["status"], "RESOLVED")
        self.assertIn("if rate else 0.0", (self.project.root / "app.py").read_text())
        self.assertIsNone(resolved["resolution"]["settlement"])
        self.assertTrue(any("No on-chain settlement" in n for n in resolved["resolution"]["notes"]))

    def test_llm_forge_is_reported_as_disabled_without_a_key(self):
        incident = self.run_incident(self.orchestrator())
        llm = next(c for c in incident["candidates"] if c["strategy"] == "llm")
        self.assertEqual(llm["declined"], "No LLM API key configured")

    def test_score_drops_when_no_test_reproduces_the_crash(self):
        tests = (self.project.root / "tests" / "test_app.py")
        tests.write_text(tests.read_text().replace(
            '    def test_unknown_currency(self):\n        self.assertEqual(app.convert(100, "XYZ"), 0.0)\n\n', ""))
        incident = self.run_incident(self.orchestrator())
        guard = incident["candidates"][0]["guard"]
        self.assertTrue(guard["passed"])
        self.assertEqual(guard["score"], 80)

    def test_patch_that_fails_the_suite_is_not_offered(self):
        tests = (self.project.root / "tests" / "test_app.py")
        tests.write_text(tests.read_text().replace('app.convert(100, "XYZ"), 0.0)', 'app.convert(100, "XYZ"), -1)'))
        incident = self.run_incident(self.orchestrator())
        self.assertEqual(incident["status"], "NO_FIX")
        self.assertFalse(incident["candidates"][0]["guard"]["passed"])
        with self.assertRaises(IncidentError):
            self.orchestrator().approve(incident["incident_id"], "tester")

    def test_malicious_llm_patch_is_blocked_before_it_runs(self):
        marker = self.root / "pwned"
        evil = json.dumps({"rationale": "fix", "edits": [{
            "search": "    rate = RATES.get(currency, 0.0)",
            "replace": f"    __import__('os').system('touch {marker}')\n    rate = RATES.get(currency, 1.0)",
        }]})
        incident = self.run_incident(self.orchestrator(llm=ScriptedLLM(evil)))
        llm = next(c for c in incident["candidates"] if c["strategy"] == "llm")
        self.assertFalse(llm["guard"]["passed"])
        self.assertFalse(llm["guard"]["executed"])
        self.assertIsNone(llm["forge_run"])
        self.assertFalse(marker.exists(), "the blocked patch must never have been executed")
        self.assertEqual(incident["winner"], "rules")

    def test_two_forge_agents_compete_and_the_smaller_patch_wins_a_tie(self):
        bigger = json.dumps({"rationale": "explicit guard", "edits": [{
            "search": "    return round(amount / rate, 2)",
            "replace": "    if not rate:\n        return 0.0\n    return round(amount / rate, 2)",
        }]})
        incident = self.run_incident(self.orchestrator(llm=ScriptedLLM(bigger)))
        scores = {c["strategy"]: c["guard"]["score"] for c in incident["candidates"]}
        self.assertEqual(scores, {"rules": 100, "llm": 100})
        self.assertEqual(incident["winner"], "rules")

    def test_llm_wins_when_no_rule_applies(self):
        source = APP.replace('return user["plan"]', 'return user["plan"].strip()')
        source = source.replace("amount / rate", "(amount / rate if rate else 0.0)")  # only one bug in play
        self.project = make_project(self.root / "b", app_source=source)
        self.settings = make_settings(self.root, self.project)
        fix = json.dumps({"rationale": "handle missing plan", "edits": [{
            "search": 'return user["plan"].strip()', "replace": 'return (user["plan"] or "free").strip()'}]})
        orch = self.orchestrator(llm=ScriptedLLM(fix))
        payload = crash_payload(self.project, 'user["plan"]', "AttributeError",
                                "'NoneType' object has no attribute 'strip'")
        incident = orch.report(payload)
        orch.wait_idle()
        incident = orch.store.get(incident["incident_id"])
        self.assertEqual(incident["winner"], "llm")
        self.assertTrue(incident["candidates"][0]["declined"])

    def test_repeat_crashes_are_deduplicated_while_open(self):
        orch = self.orchestrator()
        first = self.run_incident(orch)
        again = orch.report(crash_payload(self.project, *ZERO_DIV))
        self.assertEqual(again["incident_id"], first["incident_id"])
        self.assertEqual(again["occurrences"], 2)

    def test_approval_is_refused_if_the_file_changed_after_verification(self):
        orch = self.orchestrator()
        incident = self.run_incident(orch)
        target = self.project.root / "app.py"
        target.write_text(target.read_text() + "\n# edited by a human\n")
        with self.assertRaisesRegex(IncidentError, "changed since"):
            orch.approve(incident["incident_id"], "tester")
        self.assertNotIn("if rate else 0.0", target.read_text())

    def test_reject_leaves_the_code_alone(self):
        orch = self.orchestrator()
        incident = self.run_incident(orch)
        self.assertEqual(orch.reject(incident["incident_id"], "tester")["status"], "REJECTED")
        self.assertEqual((self.project.root / "app.py").read_text(), APP)
        with self.assertRaises(IncidentError):
            orch.approve(incident["incident_id"], "tester")

    def test_frames_outside_registered_projects_are_refused(self):
        orch = self.orchestrator()
        with self.assertRaisesRegex(IncidentError, "registered project"):
            orch.report({"error_type": "ValueError", "error_message": "x",
                         "frames": [{"file": "/etc/passwd", "line": 1}, {"file": __file__, "line": 5}]})
        with self.assertRaises(IncidentError):
            orch.report({"error_type": "x; rm -rf /", "error_message": "x",
                         "frames": [{"file": str(self.project.root / "app.py"), "line": 6}]})

    def test_incidents_survive_a_restart(self):
        orch = self.orchestrator()
        incident = self.run_incident(orch)
        orch.stop()
        restarted = self.orchestrator()
        self.assertEqual(restarted.approve(incident["incident_id"], "tester")["status"], "RESOLVED")


class ChainOrderingTests(PipelineCase):
    def test_every_agent_signs_its_own_step_in_order(self):
        escrow = RecordingEscrow([self.project.repo])
        orch = self.orchestrator(escrow=escrow)
        incident = self.run_incident(orch)
        self.assertEqual([c[0] for c in escrow.calls], ["open_incident", "submit_recon", "submit_patch", "attest"])
        self.assertEqual([t["signer"] for t in incident["chain"]["txs"]],
                         ["operator.test", "recon.test", "forge-rules.test", "guard.test"])
        patch_hash = incident["candidates"][0]["patch_hash"]
        self.assertEqual(escrow.calls[2], ("submit_patch", "rules", patch_hash, 3, 0))

        resolved = orch.approve(incident["incident_id"], "tester")
        self.assertEqual(escrow.calls[-1], ("approve", "rules", patch_hash))
        self.assertEqual(resolved["resolution"]["settlement"]["signer_id"], "owner.test")
        self.assertEqual(resolved["chain"]["txs"][-1]["step"], "approve")
        self.assertEqual([p["amount_near"] for p in resolved["resolution"]["payouts"]], ["0.175", "0.0375", "0.0375"])

    def test_blocked_patch_is_recorded_on_chain_as_failed(self):
        evil = json.dumps({"edits": [{"search": "    rate = RATES.get(currency, 0.0)",
                                      "replace": "    eval('1')\n    rate = RATES.get(currency, 0.0)"}]})
        escrow = RecordingEscrow([self.project.repo])
        self.run_incident(self.orchestrator(escrow=escrow, llm=ScriptedLLM(evil)))
        self.assertIn(("attest", "llm", False, 0), escrow.calls)

    def test_bounty_is_returned_when_nothing_passes_or_on_reject(self):
        escrow = RecordingEscrow([self.project.repo])
        orch = self.orchestrator(escrow=escrow)
        incident = self.run_incident(orch)
        orch.reject(incident["incident_id"], "tester")
        self.assertEqual(escrow.calls[-1][0], "reject")
        self.assertEqual(escrow.incidents[incident["incident_id"]]["status"], "Rejected")

    def test_unregistered_treasury_is_reported_not_faked(self):
        orch = self.orchestrator(escrow=RecordingEscrow(["someone/else"]))
        incident = self.run_incident(orch)
        self.assertFalse(incident["chain"]["enabled"])
        self.assertEqual(incident["chain"]["txs"], [])
        self.assertIn("No treasury registered", incident["chain"]["error"])


class ServerTests(PipelineCase):
    def start(self, **overrides):
        for key, value in overrides.items():
            setattr(self.settings, key, value)
        self.settings.port = 0
        orch = self.orchestrator()
        server = MendServer(self.settings, orch)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"
        return orch

    def request(self, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        merged = {"Content-Type": "application/json"} if data is not None else {}
        merged.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=merged, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_report_approve_over_http(self):
        orch = self.start()
        status, body = self.request("/api/v1/incidents", crash_payload(self.project, *ZERO_DIV))
        self.assertEqual(status, 202)
        orch.wait_idle()
        status, incident = self.request(f"/api/v1/incidents/{body['incident_id']}")
        self.assertEqual(incident["status"], "AWAITING_APPROVAL")
        self.assertNotIn("patched_source", incident["candidates"][0])
        status, resolved = self.request(f"/api/v1/incidents/{body['incident_id']}/approve", {"approver": "t"})
        self.assertEqual((status, resolved["status"]), (200, "RESOLVED"))
        status, _ = self.request(f"/api/v1/incidents/{body['incident_id']}/approve", {})
        self.assertEqual(status, 409)

    def test_token_is_required_when_configured(self):
        self.start(ingest_token="s3cret-token")
        payload = crash_payload(self.project, *ZERO_DIV)
        self.assertEqual(self.request("/api/v1/incidents", payload)[0], 401)
        self.assertEqual(self.request("/api/v1/incidents", payload, {"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.request("/api/v1/incidents")[0], 401)
        self.assertEqual(self.request("/api/v1/incidents", payload, {"Authorization": "Bearer s3cret-token"})[0], 202)
        self.assertEqual(self.request("/health")[0], 200)

    def test_browser_cross_site_posts_are_refused(self):
        self.start()
        payload = crash_payload(self.project, *ZERO_DIV)
        self.assertEqual(self.request("/api/v1/incidents", payload, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/v1/incidents", raw=json.dumps(payload).encode(),
                                      headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("/api/v1/incidents", payload, {"Host": "evil.example"})[0], 403)

    def test_bad_input_is_rejected(self):
        self.start()
        self.assertEqual(self.request("/api/v1/incidents", raw=b"{not json")[0], 400)
        status, body = self.request("/api/v1/incidents", {"error_type": "ValueError", "frames": [{"file": "/etc/passwd", "line": 1}]})
        self.assertEqual(status, 400)
        self.assertEqual(self.request("/api/v1/incidents/INC-NOPE0000")[0], 404)
        self.assertEqual(self.request("/api/v1/incidents/../../etc/passwd")[0], 404)

    def test_a_test_command_in_the_payload_is_ignored(self):
        orch = self.start()
        marker = self.root / "ran"
        payload = dict(crash_payload(self.project, *ZERO_DIV), test_command=f"touch {marker}")
        self.request("/api/v1/incidents", payload)
        orch.wait_idle()
        self.assertFalse(marker.exists())

    def test_refuses_public_bind_without_a_token(self):
        from mend.server import build
        self.settings.host = "0.0.0.0"
        with self.assertRaises(SystemExit):
            build(self.settings)


if __name__ == "__main__":
    unittest.main()


class TelegramTapTests(PipelineCase):
    def test_second_tap_is_answered_quietly_and_buttons_are_removed(self):
        from mend.bot.telegram_bot import TelegramBot
        orch = self.orchestrator()
        incident = self.run_incident(orch)
        bot = TelegramBot("t", "42", [], orch)
        calls = []
        bot.api = lambda method, data=None, timeout=20.0: calls.append((method, data)) or {"ok": True, "result": {"message_id": 1}}
        tap = {"id": "q", "data": f"approve:{incident['incident_id']}", "from": {"id": 7, "username": "dev"},
               "message": {"chat": {"id": 42}, "message_id": 9}}
        bot.handle_callback(tap)
        self.assertIn("editMessageReplyMarkup", [m for m, _ in calls])
        for _ in range(100):
            if orch.store.get(incident["incident_id"])["status"] == "RESOLVED":
                break
            import time; time.sleep(0.05)
        calls.clear()
        bot.handle_callback(tap)
        self.assertEqual([m for m, _ in calls], ["answerCallbackQuery"])
        self.assertIn("already resolved", calls[0][1]["text"])

    def test_taps_from_other_chats_are_refused(self):
        from mend.bot.telegram_bot import TelegramBot
        orch = self.orchestrator()
        incident = self.run_incident(orch)
        bot = TelegramBot("t", "42", ["7"], orch)
        calls = []
        bot.api = lambda method, data=None, timeout=20.0: calls.append((method, data)) or {"ok": True}
        bot.handle_callback({"id": "q", "data": f"approve:{incident['incident_id']}", "from": {"id": 8},
                             "message": {"chat": {"id": 42}, "message_id": 9}})
        self.assertTrue(calls[0][1]["show_alert"])
        self.assertEqual(orch.store.get(incident["incident_id"])["status"], "AWAITING_APPROVAL")
