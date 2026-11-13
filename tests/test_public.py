"""
The public service, end to end: a Telegram user runs /try and approves the fix.
Git, the sandbox and the swarm are real; the GitHub API, Telegram and NEAR are test doubles.
"""
import shutil
import subprocess
import time
import unittest
from pathlib import Path

from mend.accounts import AccountStore, Vault
from mend.bot.telegram_bot import TelegramBot
from mend.config import Settings
from mend.hosted import HostedService
from mend.swarm.orchestrator import SwarmOrchestrator
import json

from tests.helpers import RecordingEscrow, ScriptedLLM, temp_dir

FIXTURE = Path(__file__).parent / "fixtures" / "demo_shop"


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True)


class FakeGitHub:
    """GitHub API double. `prs` is shared so tests can see which token opened which PR."""

    prs = []

    def __init__(self, token=""):
        self.token = token

    def get_repo(self, repo):
        return {"ok": True, "full_name": repo, "default_branch": "main", "private": False,
                "clone_url": "unused", "can_push": repo.startswith(self.token.split(":")[0] + "/")}

    def fork(self, repo):
        return {"ok": True, "full_name": f"{self.token.split(':')[0]}/mend-demo-shop", "default_branch": "main"}

    def user(self):
        return {"ok": True, "login": self.token.split(":")[0]}

    def open_fix_pull_request(self, **kw):
        FakeGitHub.prs.append((self.token, kw))
        return {"ok": True, "pr_url": f"https://github.com/{kw['repo']}/pull/1", "pr_number": 1, "branch": kw["branch"]}

    def comment(self, repo, number, body):
        return {"ok": True}


class PublicServiceTests(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        seed = root / "seed"
        shutil.copytree(FIXTURE, seed)
        git(seed, "init", "-q", "-b", "main")
        git(seed, "add", ".")
        git(seed, "commit", "-q", "-m", "demo shop")
        self.remote = root / "remote.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(self.remote)], check=True)

        self.settings = Settings(base_dir=root, db_path=root / "mend.db", public=True,
                                 connected_projects_file=root / ".mend" / "projects.json",
                                 workspaces_dir=root / ".mend" / "workspaces", github_client_id="cid",
                                 bounty_near="0.05", limit_tries_per_day=1)
        self.accounts = AccountStore(self.settings.db_path, Vault(Vault.new_key()))
        self.escrow = RecordingEscrow([])
        fix = json.dumps({"rationale": "Treat a malformed coupon as no discount.", "edits": [{
            "search": "    percent = int(code[4:])\n",
            "replace": "    try:\n        percent = int(code[4:])\n    except ValueError:\n        return 0\n"}]})
        self.orch = SwarmOrchestrator(self.settings, escrow=self.escrow, accounts=self.accounts, llm=ScriptedLLM(fix))
        self.orch.start()
        self.addCleanup(self.orch.stop)
        self.hosted = HostedService(self.settings, self.orch, self.accounts)
        self.hosted.clone_url_for = lambda repo: str(self.remote)

        # Route every GitHub client the code creates to the double.
        import mend.connect, mend.hosted, mend.swarm.orchestrator
        for module in (mend.connect, mend.hosted, mend.swarm.orchestrator):
            self._patch(module, "GitHubClient", FakeGitHub)
        FakeGitHub.prs = []
        import mend.integrations.escrow as escrow_module
        self._patch(escrow_module, "register_repo_treasury",
                    lambda *a, **k: {"balance": str(15 * 10**22), "max_bounty": str(10**23)})
        self.settings.near_keys_file = root / "keys.json"
        self.settings.near_keys_file.write_text("{}")

        self.bot = TelegramBot("token", "", [], self.orch, hosted=self.hosted)
        self.sent = []

        def api(method, data=None, timeout=20.0):
            self.sent.append((method, data or {}))
            return {"ok": True, "result": {"message_id": len(self.sent)}}

        self.bot.api = api

    def _patch(self, module, name, value):
        original = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, original)

    def say(self, user_id, text):
        self.bot.handle_message({"chat": {"id": user_id, "type": "private"}, "from": {"id": user_id, "username": f"u{user_id}"},
                                 "text": text})

    def texts(self, chat_id):
        return [d.get("text", "") for m, d in self.sent if m in ("sendMessage", "editMessageText") and str(d.get("chat_id")) == str(chat_id)]

    def choose_mend_funding(self, user_id):
        """Taps "Let Mend fund it" on the funding message."""
        def button():
            for m, d in self.sent:
                for row in (d.get("reply_markup") or {}).get("inline_keyboard", []):
                    for b in row:
                        if str(b.get("callback_data", "")).startswith("fund:") and str(d.get("chat_id")) == str(user_id):
                            return b["callback_data"]
            return None
        self.assertTrue(self.wait_for(lambda: button() is not None), self.texts(user_id))
        self.bot.handle_callback({"id": "q", "data": button(), "from": {"id": user_id},
                                  "message": {"chat": {"id": user_id}, "message_id": 3}})

    def wait_for(self, predicate, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.1)
        return False

    def test_a_stranger_can_try_the_whole_loop_and_only_they_can_approve(self):
        self.say(111, "/start")
        self.assertTrue(any("Try it in two minutes" in t for t in self.texts(111)))
        self.assertIn("deleteMessage", [m for m, _ in self.sent])  # the keyboard-removal note is cleaned up

        self.say(111, "/try")
        self.assertIn("Connect GitHub first", self.texts(111)[-1])

        self.accounts.set_github("111", "alice", "alice:token")
        self.say(111, "/try")
        self.choose_mend_funding(111)
        self.assertTrue(self.wait_for(lambda: any("Mend caught it" in t for t in self.texts(111))),
                        self.texts(111))
        self.orch.wait_idle()
        incident = self.orch.store.list(1)[0]
        self.assertEqual(incident["repo"], "alice/mend-demo-shop")
        self.assertEqual(incident["error_type"], "ValueError")
        self.assertEqual(incident["owner_telegram_id"], "111")
        self.assertEqual(incident["status"], "AWAITING_APPROVAL", incident.get("error"))
        self.assertEqual(self.escrow.calls[0], ("open_incident", incident["incident_id"], "0.05"))
        self.assertTrue(any("Fix ready for review" in t for t in self.texts(111)))

        # Someone else cannot approve Alice's fix, even with the right button data.
        tap = lambda uid: self.bot.handle_callback({"id": "q", "data": f"approve:{incident['incident_id']}",
                                                    "from": {"id": uid, "username": f"u{uid}"},
                                                    "message": {"chat": {"id": uid}, "message_id": 5}})
        tap(222)
        self.assertEqual(self.orch.store.get(incident["incident_id"])["status"], "AWAITING_APPROVAL")

        tap(111)
        self.assertTrue(self.wait_for(lambda: self.orch.store.get(incident["incident_id"])["status"] == "RESOLVED"))
        token, pr = FakeGitHub.prs[0]
        self.assertEqual(token, "alice:token")  # opened with Alice's own GitHub access
        self.assertEqual(pr["repo"], "alice/mend-demo-shop")
        self.assertIn("except ValueError", pr["patched_source"])
        self.assertEqual(self.escrow.calls[-1][0], "approve")

    def test_daily_limit_and_one_request_at_a_time(self):
        self.accounts.set_github("111", "alice", "alice:token")
        self.say(111, "/try")
        self.say(111, "/try")
        self.assertTrue(any("Still working" in t or "times today" in t for t in self.texts(111)))

    def test_project_tokens_only_report_for_their_own_project(self):
        self.accounts.set_github("111", "alice", "alice:token")
        self.say(111, "/watch alice/mend-demo-shop")
        self.assertTrue(self.wait_for(lambda: any("is protected" in t for t in self.texts(111))), self.texts(111))
        message = next(t for t in self.texts(111) if "is protected" in t)
        self.assertIn('token="mend_', message)
        project = self.settings.projects[0]
        self.assertEqual(project.owner_telegram_id, "111")
        self.assertTrue(project.ingest_token_hash)

    def test_cannot_watch_someone_elses_repo(self):
        self.accounts.set_github("111", "alice", "alice:token")
        self.say(111, "/watch bob/private-stuff")
        self.assertTrue(self.wait_for(lambda: any("cannot push" in t for t in self.texts(111))), self.texts(111))

    def test_group_chats_are_ignored(self):
        self.bot.handle_message({"chat": {"id": -5, "type": "group"}, "from": {"id": 1}, "text": "/start"})
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()


class OwnerTriesOwnDemo(PublicServiceTests):
    def test_the_demo_owner_uses_the_original_repo(self):
        import mend.hosted
        self.accounts.set_github("111", mend.hosted.DEMO_UPSTREAM.split("/")[0], "Rampop01:token")
        self.say(111, "/try")
        self.choose_mend_funding(111)
        self.assertTrue(self.wait_for(lambda: any("Mend caught it" in t for t in self.texts(111))), self.texts(111))
        self.assertEqual(self.orch.store.list(1)[0]["repo"], mend.hosted.DEMO_UPSTREAM)


class SignInAndLegacyButtons(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.settings = Settings(base_dir=root, db_path=root / "m.db", public=True, github_client_id="cid")
        self.accounts = AccountStore(self.settings.db_path, Vault(Vault.new_key()))
        self.orch = SwarmOrchestrator(self.settings, accounts=self.accounts)
        self.hosted = HostedService(self.settings, self.orch, self.accounts)
        self.bot = TelegramBot("t", "", [], self.orch, hosted=self.hosted)
        self.sent = []
        self.bot.api = lambda m, d=None, timeout=20.0: self.sent.append((m, d or {})) or {"ok": True, "result": {"message_id": 1}}

    def test_old_keyboard_buttons_are_mapped_and_removed(self):
        import mend.accounts
        original = mend.accounts.DeviceFlow.start
        mend.accounts.DeviceFlow.start = lambda self: {"device_code": "dc", "user_code": "ABCD-1234",
                                                       "verification_uri": "https://github.com/login/device",
                                                       "interval": 5, "expires_in": 900}
        self.addCleanup(setattr, mend.accounts.DeviceFlow, "start", original)
        import mend.hosted
        self.hosted._wait_for_login = lambda *a, **k: None
        self.bot.handle_message({"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "🐙 Connect GitHub"})
        markups = [d.get("reply_markup") for m, d in self.sent if m == "sendMessage"]
        self.assertIn({"remove_keyboard": True}, markups)
        self.assertTrue(any("ABCD-1234" in d.get("text", "") for m, d in self.sent))

    def test_a_sign_in_in_progress_survives_a_restart(self):
        self.accounts.save_pending("7", "device-secret", "ABCD-1234", 5, 900)
        resumed = []
        self.hosted._wait_for_login = lambda uid, code, interval, exp, notify: resumed.append((uid, code))
        self.assertEqual(self.hosted.resume_logins(lambda uid: print), 1)
        self.assertEqual(resumed, [("7", "device-secret")])
        # Trying the demo meanwhile reminds the user of their code.
        self.bot.handle_message({"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "/try"})
        self.assertIn("ABCD-1234", self.sent[-1][1]["text"])


class WalletOwnedTreasury(PublicServiceTests):
    def test_the_users_wallet_funds_and_approves_and_telegram_taps_cannot(self):
        self.settings.public_url = "https://mend.example"
        self.settings.secret_key = "k" * 32
        self.accounts.set_github("111", "alice", "alice:token")
        self.say(111, "/try")
        funding = lambda: next((d for m, d in self.sent if "Fund the bounty treasury" in d.get("text", "")), None)
        self.assertTrue(self.wait_for(lambda: funding() is not None), self.texts(111))
        wallet_button = funding()["reply_markup"]["inline_keyboard"][0][0]
        self.assertTrue(wallet_button["url"].startswith("https://mend.example/wallet?t="))

        # The page claims the wallet funded it, but the chain shows nothing yet: refused.
        from mend.hosted import ServiceError
        with self.assertRaises(ServiceError):
            self.hosted.wallet_funded("111", "alice/mend-demo-shop", "try", "alice.testnet")
        # Someone else's account owns the treasury: refused.
        self.escrow.register("alice/mend-demo-shop", owner="mallory.testnet")
        with self.assertRaises(ServiceError):
            self.hosted.wallet_funded("111", "alice/mend-demo-shop", "try", "alice.testnet")
        # Alice's wallet really funded it: accepted, and the demo continues.
        self.escrow.register("alice/mend-demo-shop", owner="alice.testnet")
        self.hosted.wallet_funded("111", "alice/mend-demo-shop", "try", "alice.testnet")
        self.assertTrue(self.wait_for(lambda: any("Mend caught it" in t for t in self.texts(111))), self.texts(111))
        self.orch.wait_idle()
        incident = self.orch.store.list(1)[0]
        self.assertEqual(incident["near_owner"], "alice.testnet")

        card = next(d for m, d in reversed(self.sent) if "Fix ready for review" in d.get("text", ""))
        approve = card["reply_markup"]["inline_keyboard"][0][0]
        self.assertIn("/wallet?t=", approve["url"])  # a wallet signature, not a Telegram tap
        from mend.swarm.orchestrator import IncidentError
        with self.assertRaises(IncidentError):
            self.orch.approve(incident["incident_id"], "telegram")

        # Before the wallet signs, finalising is refused; after the chain shows the payout, the PR is opened.
        with self.assertRaises(IncidentError):
            self.orch.finalize_wallet_approval(incident["incident_id"], "alice.testnet", "tx1")
        winner = next(c for c in incident["candidates"] if c["strategy"] == incident["winner"])
        self.escrow.approve(incident["incident_id"], winner["strategy"], winner["patch_hash"])
        with self.assertRaises(IncidentError):
            self.orch.finalize_wallet_approval(incident["incident_id"], "mallory.testnet", "tx1")
        done = self.orch.finalize_wallet_approval(incident["incident_id"], "alice.testnet", "tx1")
        self.assertEqual(done["status"], "RESOLVED")
        self.assertEqual(done["resolution"]["pr_url"], "https://github.com/alice/mend-demo-shop/pull/1")
        self.assertEqual(done["resolution"]["settlement"]["signer_id"], "alice.testnet")


class WalletLinking(PublicServiceTests):
    def test_welcome_offers_the_wallet_and_linking_shows_the_balance(self):
        self.settings.public_url = "https://mend.example"
        self.settings.secret_key = "k" * 32
        self.say(111, "/start")
        welcome = next(d for m, d in self.sent if "Try it in two minutes" in d.get("text", ""))
        buttons = [row[0] for row in welcome["reply_markup"]["inline_keyboard"]]
        self.assertEqual([b["text"][:2] for b in buttons], ["1.", "2.", "3."])
        self.assertIn("/wallet?t=", buttons[1]["url"])

        result = self.hosted.wallet_linked("111", "alice.testnet")
        self.assertEqual(self.accounts.near_account("111"), "alice.testnet")
        self.assertEqual(result["balance"], str(5 * 10**24))
        self.assertTrue(any("NEAR wallet linked" in t and "5 NEAR" in t for t in self.texts(111)))

        self.say(111, "/wallet")
        self.assertIn("alice.testnet", self.texts(111)[-1])


class WatchConversation(PublicServiceTests):
    def test_repo_from_text(self):
        f = TelegramBot.repo_from_text
        self.assertEqual(f("rampop01/https://github.com/Rampop01/Aegis-backend-ai"), "Rampop01/Aegis-backend-ai")
        self.assertEqual(f("https://github.com/a-b/c.d.git"), "a-b/c.d")
        self.assertEqual(f("  alice/shop "), "alice/shop")
        self.assertEqual(f("git@github.com:alice/shop.git"), "alice/shop")
        self.assertIsNone(f("hello there"))

    def test_watch_asks_for_the_repo_then_uses_the_next_message(self):
        self.accounts.set_github("111", "alice", "alice:token")
        self.say(111, "/watch")
        self.assertIn("Which repository", self.texts(111)[-1])
        self.say(111, "https://github.com/alice/mend-demo-shop")
        self.assertTrue(self.wait_for(lambda: any("is protected" in t for t in self.texts(111))), self.texts(111))

    def test_pasted_link_starts_watch_and_other_text_gets_a_short_hint(self):
        self.say(111, "hello")
        self.assertIn("paste its link", self.texts(111)[-1])
        self.assertFalse(any("Try it in two minutes" in t for t in self.texts(111)))
        self.accounts.set_github("111", "alice", "alice:token")
        self.say(111, "please protect https://github.com/alice/mend-demo-shop")
        self.assertTrue(self.wait_for(lambda: any("is protected" in t for t in self.texts(111))), self.texts(111))


class FaucetDrip(PublicServiceTests):
    def setUp(self):
        super().setUp()
        sent = self.drips = []
        balances = self.balances = {"alice.testnet": 0, "bob.testnet": 0, "rich.testnet": 5 * 10**24}

        class Owner:
            def balance(self_inner):
                return self.owner_balance

        class Rpc(RecordingEscrow._Rpc):
            def view_account(self_inner, account_id):
                return {"amount": str(balances.get(account_id, 0))} if account_id in balances else None

        self.owner_balance = 4 * 10**24
        self.escrow._accounts = {"owner": Owner()}
        self.escrow.rpc = Rpc()

        def send_near(account_id, amount_near):
            from mend.integrations.near import near_to_yocto, TxResult
            sent.append((account_id, amount_near))
            balances[account_id] += near_to_yocto(amount_near)
            return TxResult("tx", "owner.test", account_id, "b", "https://example.invalid/txns/tx")

        self.escrow.send_near = send_near

    def test_drip_once_per_person_and_per_wallet(self):
        self.accounts.set_github("111", "alice", "alice:token")
        self.accounts.set_near_account("111", "alice.testnet")
        self.say(111, "/faucet")
        self.assertTrue(self.wait_for(lambda: any("Sent." in t for t in self.texts(111))), self.texts(111))
        self.assertEqual(self.drips, [("alice.testnet", "0.3")])

        self.balances["alice.testnet"] = 0  # spent it; still only once
        self.say(111, "/faucet")
        self.assertIn("already received", self.texts(111)[-1])

        # Another person pointing at the same wallet gets nothing either.
        self.accounts.set_github("222", "bob", "bob:token")
        self.accounts.upsert("222", "222", "bob")
        self.accounts.set_near_account("222", "alice.testnet")
        self.say(222, "/faucet")
        self.assertIn("already received", self.texts(222)[-1])
        self.assertEqual(len(self.drips), 1)

    def test_no_drip_without_github_to_a_funded_wallet_or_below_the_reserve(self):
        self.accounts.upsert("333", "333", "x")
        self.accounts.set_near_account("333", "bob.testnet")
        self.say(333, "/faucet")
        self.assertIn("Connect GitHub first", self.texts(333)[-1])

        self.accounts.set_github("444", "rich", "rich:token")
        self.accounts.set_near_account("444", "rich.testnet")
        self.say(444, "/faucet")
        self.assertIn("already has", self.texts(444)[-1])

        self.accounts.set_github("555", "bob", "bob:token")
        self.accounts.set_near_account("555", "bob.testnet")
        self.owner_balance = near_yocto = 17 * 10**23  # 1.7 NEAR: a drip would go below the 1.5 reserve
        self.say(555, "/faucet")
        self.assertIn("run out", self.texts(555)[-1])
        self.assertEqual(self.drips, [])
