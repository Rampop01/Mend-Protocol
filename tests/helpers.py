"""Shared fixtures: a throwaway project with a real bug and a real test suite."""
from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

from mend.config import Project, Settings
from mend.integrations.near import TxResult

APP = textwrap.dedent(
    '''
    RATES = {"USD": 1.0, "EUR": 1.08}


    def convert(amount, currency):
        rate = RATES.get(currency, 0.0)
        return round(amount / rate, 2)


    def plan_name(user):
        return user["plan"]


    def city_of(profile):
        return profile.address.upper()
    '''
).lstrip()

TESTS = textwrap.dedent(
    '''
    import sys, unittest
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import app


    class T(unittest.TestCase):
        def test_usd(self):
            self.assertEqual(app.convert(100, "USD"), 100.0)

        def test_unknown_currency(self):
            self.assertEqual(app.convert(100, "XYZ"), 0.0)

        def test_plan(self):
            self.assertEqual(app.plan_name({"plan": "pro"}), "pro")


    if __name__ == "__main__":
        unittest.main()
    '''
).lstrip()


def make_project(tmp: Path, app_source: str = APP, tests_source: str = TESTS) -> Project:
    root = tmp / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "app.py").write_text(app_source, encoding="utf-8")
    (root / "tests" / "test_app.py").write_text(tests_source, encoding="utf-8")
    return Project(
        repo="acme/shop",
        root=root.resolve(),
        test_command=[sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        test_timeout=60,
    )


def make_settings(tmp: Path, project: Project, **overrides: Any) -> Settings:
    settings = Settings(base_dir=tmp, db_path=tmp / "mend.db", projects=[project])
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def line_of(project: Project, needle: str, rel_path: str = "app.py") -> int:
    for number, line in enumerate((project.root / rel_path).read_text().splitlines(), start=1):
        if needle in line:
            return number
    raise AssertionError(f"{needle!r} not found")


def crash_payload(project: Project, needle: str, error_type: str, message: str,
                  frame_locals: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    return {
        "error_type": error_type,
        "error_message": message,
        "repo": project.repo,
        "frames": [
            {"file": "/usr/lib/python3/somewhere/else.py", "line": 3, "function": "outer"},
            {"file": str(project.root / "app.py"), "line": line_of(project, needle), "function": "f",
             "locals": frame_locals or {}},
        ],
        "traceback": f"Traceback (most recent call last):\n{error_type}: {message}\n",
    }


class ScriptedLLM:
    """Test double for the LLM provider: returns a fixed reply. Used only in tests."""

    configured = True

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: List[str] = []

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> str:
        self.prompts.append(user)
        return self.reply


class RecordingEscrow:
    """Test double for the contract client: records calls and enforces the contract's key rules."""

    network = "testnet"
    contract_id = "escrow.test"

    class _Rpc:
        def tx_url(self, h): return f"https://example.invalid/txns/{h}"
        def account_url(self, a): return f"https://example.invalid/address/{a}"
        def view_account(self, a): return {"amount": str(5 * 10**24)}

    def __init__(self, repos: List[str]) -> None:
        self.repos = repos
        self.calls: List[tuple] = []
        self.incidents: Dict[str, Dict[str, Any]] = {}
        self.treasuries: Dict[str, Dict[str, Any]] = {}
        self.rpc = RecordingEscrow._Rpc()

    def account_id(self, role: str) -> str:
        return f"{role}.test"

    def get_repo(self, repo_id):
        return self.treasuries.get(repo_id)

    def register(self, repo_id, owner="owner.test"):
        self.treasuries[repo_id] = {"owner": owner, "operator": "operator.test", "guard_agent": "guard.test",
                                    "balance": str(2 * 10**23), "reserved": "0", "total_paid": "0"}
        if repo_id not in self.repos:
            self.repos.append(repo_id)

    def forge_account_id(self, strategy: str) -> str:
        return f"forge-{strategy}.test"

    def _tx(self, signer: str, method: str) -> TxResult:
        n = len(self.calls)
        return TxResult(tx_hash=f"tx{n}", signer_id=signer, receiver_id=self.contract_id,
                        block_hash="b", explorer_url=f"https://example.invalid/txns/tx{n}")

    def open_incident(self, incident_id, repo_id, crash_hash, bounty_near):
        self.calls.append(("open_incident", incident_id, bounty_near))
        self.incidents[incident_id] = {"status": "Open", "subs": {}, "payouts": []}
        return self._tx("operator.test", "open_incident")

    def submit_recon(self, incident_id, report_hash):
        assert len(report_hash) == 64
        self.calls.append(("submit_recon", incident_id))
        return self._tx("recon.test", "submit_recon")

    def submit_patch(self, strategy, incident_id, patch_hash, tests_run, tests_failed):
        self.calls.append(("submit_patch", strategy, patch_hash, tests_run, tests_failed))
        self.incidents[incident_id]["subs"][strategy] = {"patch_hash": patch_hash, "failed": tests_failed, "run": tests_run}
        return self._tx(f"forge-{strategy}.test", "submit_patch")

    def attest(self, incident_id, strategy, patch_hash, passed, score, report_hash):
        sub = self.incidents[incident_id]["subs"][strategy]
        assert sub["patch_hash"] == patch_hash
        sub["attested"] = passed
        self.calls.append(("attest", strategy, passed, score))
        return self._tx("guard.test", "attest")

    def approve(self, incident_id, strategy, patch_hash):
        inc = self.incidents[incident_id]
        sub = inc["subs"][strategy]
        assert inc["status"] == "Open" and sub["patch_hash"] == patch_hash and sub["attested"] and sub["failed"] == 0
        inc["status"] = "Resolved"
        inc["winner"] = f"forge-{strategy}.test"
        inc["accepted_patch_hash"] = patch_hash
        inc["payouts"] = [
            {"account": f"forge-{strategy}.test", "role": "forge", "amount": str(175 * 10**21)},
            {"account": "guard.test", "role": "guard", "amount": str(375 * 10**20)},
            {"account": "recon.test", "role": "recon", "amount": str(375 * 10**20)},
        ]
        self.calls.append(("approve", strategy, patch_hash))
        return self._tx("owner.test", "approve")

    def reject(self, incident_id):
        self.incidents[incident_id]["status"] = "Rejected"
        self.calls.append(("reject", incident_id))
        return self._tx("operator.test", "reject")

    def get_incident(self, incident_id):
        inc = self.incidents.get(incident_id)
        return ({"status": inc["status"], "payouts": inc["payouts"], "winner": inc.get("winner"),
                 "accepted_patch_hash": inc.get("accepted_patch_hash")} if inc else None)


def temp_dir() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(prefix="mend-test-")
