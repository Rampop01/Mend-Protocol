"""Unit tests for the NEAR encoding, the forge rules, the guard and the test-output parser."""
import ast
import unittest
from pathlib import Path

from nacl.signing import VerifyKey

from mend.integrations import near
from mend.integrations.llm import extract_json_object
from mend.sandbox import parse_test_output
from mend.sdk.watcher import extract_incident_context, sanitize_frame_locals
from mend.swarm.forge import Candidate, LLMForge, RuleForge
from mend.swarm.guard import GuardAgent
from mend.swarm.recon import ReconAgent
from tests.helpers import APP, ScriptedLLM, line_of, make_project, make_settings, temp_dir


class NearEncodingTests(unittest.TestCase):
    def test_base58_round_trip_keeps_leading_zeros(self):
        for raw in (b"", b"\0\0\x01", bytes(range(32)), b"\xff" * 64):
            self.assertEqual(near.b58decode(near.b58encode(raw)), raw)

    def test_near_amounts_are_exact(self):
        self.assertEqual(near.near_to_yocto("0.25"), 250000000000000000000000)
        self.assertEqual(near.near_to_yocto("3"), 3 * 10**24)
        self.assertEqual(near.yocto_to_near(175 * 10**21), "0.175")
        self.assertEqual(near.yocto_to_near(2 * 10**24), "2")

    def test_key_round_trip_and_signature(self):
        key = near.KeyPair.generate()
        restored = near.KeyPair.from_string(key.secret_key)
        self.assertEqual(restored.public_key, key.public_key)
        signature = key.sign(b"mend")
        VerifyKey(key.public_bytes).verify(b"mend", signature)
        self.assertEqual(len(key.implicit_account_id), 64)

    def test_function_call_action_layout(self):
        action = near.function_call("attest", {"a": 1}, gas=5, deposit=7)
        self.assertEqual(action[0], 2)
        self.assertEqual(action[1:5], (6).to_bytes(4, "little"))
        self.assertEqual(action[5:11], b"attest")
        self.assertEqual(action[-24:-16], (5).to_bytes(8, "little"))
        self.assertEqual(action[-16:], (7).to_bytes(16, "little"))

    def test_function_call_key_names_only_the_allowed_methods(self):
        key = near.KeyPair.generate()
        action = near.add_function_call_key(key.public_key, "escrow.test", ["attest"], allowance=10)
        self.assertEqual(action[0], 5)
        self.assertIn(b"escrow.test", action)
        self.assertIn(b"attest", action)
        self.assertNotEqual(action[-1], 1)  # not the FullAccess permission tag


class RuleForgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir()
        self.project = make_project(Path(self.tmp.name))
        self.recon = ReconAgent(make_settings(Path(self.tmp.name), self.project))
        self.forge = RuleForge()

    def tearDown(self):
        self.tmp.cleanup()

    def _report(self, needle, error_type, message, frame_locals=None):
        return self.recon.analyze(
            self.project, self.project.root / "app.py", line_of(self.project, needle), error_type, message,
            frame_locals=frame_locals, run_baseline=False,
        )

    def test_zero_division_is_guarded(self):
        proposal = self.forge.propose(self._report("amount / rate", "ZeroDivisionError", "float division by zero"), APP)
        self.assertIn("(amount / rate if rate else 0.0)", proposal.candidate.patched_source)
        ast.parse(proposal.candidate.patched_source)

    def test_key_error_uses_get(self):
        proposal = self.forge.propose(self._report('user["plan"]', "KeyError", "'plan'"), APP)
        self.assertIn('user.get("plan")', proposal.candidate.patched_source)

    def test_key_error_for_a_different_key_is_declined(self):
        proposal = self.forge.propose(self._report('user["plan"]', "KeyError", "'other'"), APP)
        self.assertIsNone(proposal.candidate)

    def test_none_attribute_needs_the_local_to_be_none(self):
        message = "'NoneType' object has no attribute 'address'"
        declined = self.forge.propose(self._report("profile.address", "AttributeError", message), APP)
        self.assertIsNone(declined.candidate)
        proposal = self.forge.propose(
            self._report("profile.address", "AttributeError", message, {"profile": "None"}), APP
        )
        self.assertIn("if profile is None:\n        return None", proposal.candidate.patched_source)

    def test_unknown_error_types_are_declined_not_guessed(self):
        proposal = self.forge.propose(self._report("amount / rate", "RecursionError", "boom"), APP)
        self.assertIsNone(proposal.candidate)
        self.assertIn("No deterministic rule", proposal.declined)

    def test_recon_finds_the_enclosing_function(self):
        report = self._report("amount / rate", "ZeroDivisionError", "x")
        self.assertEqual(report.enclosing_scope, "def convert()")
        self.assertEqual(report.node_type, "Return")
        self.assertLessEqual(report.scope_start, report.line_number)


class LLMForgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir()
        self.project = make_project(Path(self.tmp.name))
        recon = ReconAgent(make_settings(Path(self.tmp.name), self.project))
        self.report = recon.analyze(self.project, self.project.root / "app.py",
                                    line_of(self.project, "amount / rate"), "ZeroDivisionError", "x", run_baseline=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_applies_exact_edits(self):
        reply = 'Here you go:\n{"rationale": "guard", "edits": [{"search": "return round(amount / rate, 2)", "replace": "return round(amount / rate, 2) if rate else 0.0"}]}'
        proposal = LLMForge(ScriptedLLM(reply)).propose(self.report, APP)
        self.assertIn("if rate else 0.0", proposal.candidate.patched_source)

    def test_rejects_ambiguous_or_missing_search_text(self):
        for search in ("return", "not in the file"):
            reply = '{"edits": [{"search": "%s", "replace": "x"}]}' % search
            proposal = LLMForge(ScriptedLLM(reply)).propose(self.report, APP)
            self.assertIsNone(proposal.candidate)

    def test_rejects_replies_that_do_not_parse(self):
        self.assertIsNone(LLMForge(ScriptedLLM("sorry, no")).propose(self.report, APP).candidate)
        broken = '{"edits": [{"search": "return round(amount / rate, 2)", "replace": "return (("}]}'
        self.assertIn("does not parse", LLMForge(ScriptedLLM(broken)).propose(self.report, APP).declined)

    def test_json_extraction_skips_prose_and_braces(self):
        self.assertEqual(extract_json_object('text {not json} {"a": 1} tail'), {"a": 1})
        self.assertIsNone(extract_json_object("nothing here"))


class GuardScreenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir()
        self.project = make_project(Path(self.tmp.name))
        recon = ReconAgent(make_settings(Path(self.tmp.name), self.project))
        self.report = recon.analyze(self.project, self.project.root / "app.py",
                                    line_of(self.project, "amount / rate"), "ZeroDivisionError", "x", run_baseline=False)
        self.guard = GuardAgent()

    def tearDown(self):
        self.tmp.cleanup()

    def _screen(self, old, new):
        patched = APP.replace(old, new)
        self.assertNotEqual(patched, APP)
        return self.guard.screen(self.report, Candidate("llm", "app.py", APP, patched, "r"))

    def _failed(self, report):
        return [c.name for c in report.checks if c.status == "FAIL"]

    def test_clean_patch_passes(self):
        report = self._screen("amount / rate", "(amount / rate if rate else 0.0)")
        self.assertTrue(report.passed)

    def test_blocks_shell_and_dynamic_code(self):
        for bad in ('__import__("os").system("id")', "eval(currency)", 'exec("x=1")'):
            report = self._screen("    rate = RATES", f"    {bad}\n    rate = RATES")
            self.assertIn("No dangerous calls or imports introduced", self._failed(report), bad)

    def test_blocks_new_network_and_process_imports(self):
        report = self._screen("RATES = {", "import subprocess\nRATES = {")
        self.assertFalse(report.passed)

    def test_blocks_swallowed_exceptions(self):
        report = self._screen(
            "    return round(amount / rate, 2)",
            "    try:\n        return round(amount / rate, 2)\n    except Exception:\n        pass",
        )
        self.assertIn("Does not swallow exceptions", self._failed(report))

    def test_allows_a_specific_handled_exception(self):
        report = self._screen(
            "    return round(amount / rate, 2)",
            "    try:\n        return round(amount / rate, 2)\n    except ZeroDivisionError:\n        return 0.0",
        )
        self.assertTrue(report.passed)

    def test_blocks_credentials_in_the_patch(self):
        report = self._screen("    rate = RATES", '    k = "ghp_' + "a" * 36 + '"\n    rate = RATES')
        self.assertIn("No credentials in added lines", self._failed(report))

    def test_blocks_oversized_patches(self):
        filler = "".join(f"    v{i} = {i}\n" for i in range(70))
        report = self._screen("    rate = RATES", filler + "    rate = RATES")
        self.assertIn("Blast radius within limit", self._failed(report))

    def test_existing_risky_code_is_not_blamed_on_the_patch(self):
        original = "import subprocess\n" + APP
        patched = original.replace("amount / rate", "(amount / rate if rate else 0.0)")
        report = self.guard.screen(self.report, Candidate("rules", "app.py", original, patched, "r"))
        self.assertTrue(report.passed)

    def test_refuses_to_patch_test_files(self):
        report = self.guard.screen(self.report, Candidate("llm", "tests/test_app.py", "a = 1\n", "a = 2\n", "r"))
        self.assertIn("Does not edit tests", self._failed(report))


class ParserAndSdkTests(unittest.TestCase):
    def test_unittest_output(self):
        out = "FAIL: test_x (test_app.T)\n----\nRan 4 tests in 0.001s\n\nFAILED (failures=1, errors=1)\n"
        parsed = parse_test_output(out, 1)
        self.assertEqual((parsed["tests_run"], parsed["tests_failed"]), (4, 2))
        self.assertEqual(parsed["failed_tests"], ["test_app.T.test_x"])

    def test_unittest_output_python_311_names(self):
        parsed = parse_test_output("ERROR: test_x (test_app.T.test_x)\nRan 1 test in 0.1s\nFAILED (errors=1)\n", 1)
        self.assertEqual(parsed["failed_tests"], ["test_app.T.test_x"])

    def test_pytest_output(self):
        parsed = parse_test_output("FAILED tests/test_a.py::test_b - assert 1\n==== 1 failed, 3 passed in 0.12s ====\n", 1)
        self.assertEqual((parsed["tests_run"], parsed["tests_failed"]), (4, 1))

    def test_pytest_quiet_output(self):
        parsed = parse_test_output("..........\n92 passed, 2 warnings in 0.94s\n", 0)
        self.assertEqual((parsed["tests_run"], parsed["tests_failed"]), (92, 0))
        parsed = parse_test_output("FAILED tests/t.py::test_x - boom\n1 failed, 91 passed in 1.2s\n", 1)
        self.assertEqual((parsed["tests_run"], parsed["tests_failed"], parsed["failed_tests"]), (92, 1, ["tests/t.py::test_x"]))

    def test_unknown_runner_counts_nothing(self):
        self.assertEqual(parse_test_output("ok", 0)["tests_run"], 0)

    def test_locals_are_redacted_by_name(self):
        safe = sanitize_frame_locals({"api_key": "abc", "password": "p", "amount": 5, "user": None})
        self.assertEqual(safe["api_key"], "[REDACTED]")
        self.assertEqual(safe["password"], "[REDACTED]")
        self.assertEqual((safe["amount"], safe["user"]), ("5", "None"))

    def test_sdk_payload_has_frames_with_locals(self):
        def boom(secret_token, n):
            return n / 0
        session_token = "".join(["hun", "ter2"])  # a runtime value, not source text
        try:
            boom(session_token, 3)
        except ZeroDivisionError as exc:
            payload = extract_incident_context(exc, repo="a/b")
        last = payload["frames"][-1]
        self.assertEqual(last["function"], "boom")
        self.assertEqual(last["locals"]["secret_token"], "[REDACTED]")
        self.assertNotIn("hunter2", str(payload["frames"]))


if __name__ == "__main__":
    unittest.main()
