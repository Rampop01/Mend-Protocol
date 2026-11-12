"""Tests for connected GitHub repositories, using a real git remote on disk."""
import subprocess
import sys
import unittest
from pathlib import Path

from mend.config import Settings, load_projects
from mend.connect import ConnectError, connect_repo, detect_test_command, normalize_repo
from mend.swarm.orchestrator import IncidentError, SwarmOrchestrator
from tests.helpers import APP, TESTS, RecordingEscrow, line_of, temp_dir


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


class FakeGitHub:
    """Test double for the GitHub API. The git remote itself is real."""

    token = "test-token"

    def __init__(self, repo="acme/shop", can_push=True):
        self.repo, self.can_push = repo, can_push
        self.prs, self.comments = [], []
        self.fail_pr = None

    def get_repo(self, repo):
        return {"ok": True, "full_name": self.repo, "default_branch": "main", "private": False,
                "clone_url": "unused", "can_push": self.can_push}

    def open_fix_pull_request(self, **kwargs):
        if self.fail_pr:
            return {"ok": False, "error": self.fail_pr}
        self.prs.append(kwargs)
        n = len(self.prs)
        return {"ok": True, "pr_url": f"https://github.com/{self.repo}/pull/{n}", "pr_number": n, "branch": kwargs["branch"]}

    def comment(self, repo, number, body):
        self.comments.append((number, body))
        return {"ok": True}


class ConnectTests(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # A real "GitHub" remote: a bare repository with the buggy app on main.
        seed = self.root / "seed"
        (seed / "tests").mkdir(parents=True)
        (seed / "app.py").write_text(APP)
        (seed / "tests" / "test_app.py").write_text(TESTS)
        git(seed, "init", "-q", "-b", "main")
        git(seed, "add", ".")
        git(seed, "commit", "-q", "-m", "initial")
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(self.remote)], check=True)
        self.seed = seed
        self.settings = Settings(base_dir=self.root, db_path=self.root / "mend.db",
                                 connected_projects_file=self.root / ".mend" / "projects.json",
                                 workspaces_dir=self.root / ".mend" / "workspaces")
        self.github = FakeGitHub()

    def connect(self, **kwargs):
        return connect_repo(self.settings, "https://github.com/acme/shop.git", github=self.github,
                            clone_url=str(self.remote), register_on_chain=False, log=lambda m: None, **kwargs)

    def orchestrator(self, escrow=None):
        self.settings.projects = load_projects(self.settings, self.root / "missing.json")
        orch = SwarmOrchestrator(self.settings, escrow=escrow)
        orch.github = self.github
        orch.start()
        self.addCleanup(orch.stop)
        return orch

    def crash(self, orch, workspace_path_style=True):
        project = self.settings.projects[0]
        # The crashing process runs from its own checkout, not Mend's workspace.
        reported = "/srv/deploy/current/app.py"
        incident = orch.report({"error_type": "ZeroDivisionError", "error_message": "float division by zero",
                                "repo": "acme/shop",
                                "frames": [{"file": reported, "line": line_of(project, "amount / rate"),
                                            "code": "return round(amount / rate, 2)"}]})
        orch.wait_idle()
        return orch.store.get(incident["incident_id"])

    def test_connect_clones_detects_tests_and_records_the_project(self):
        result = self.connect()
        self.assertEqual((result["repo"], result["base_branch"]), ("acme/shop", "main"))
        self.assertEqual((result["tests_run"], result["tests_failed"]), (3, 1))
        self.assertIn("unittest", result["test_command"])
        projects = load_projects(self.settings, self.root / "missing.json")
        self.assertTrue(projects[0].managed)
        self.assertNotIn("test-token", (Path(result["root"]) / ".git" / "config").read_text())

    def test_connect_refuses_repos_it_cannot_push_to(self):
        self.github = FakeGitHub(can_push=False)
        with self.assertRaisesRegex(ConnectError, "cannot push"):
            self.connect()

    def test_names_and_test_detection(self):
        self.assertEqual(normalize_repo("git@github.com:a-b/c.d.git"), "a-b/c.d")
        with self.assertRaises(ConnectError):
            normalize_repo("../../etc")
        (self.seed / "conftest.py").write_text("")
        self.assertEqual(detect_test_command(self.seed)[1:], ["-m", "pytest", "-q"])

    def test_fix_is_delivered_as_a_pull_request_and_the_workspace_is_untouched(self):
        self.connect()
        escrow = RecordingEscrow(["acme/shop"])
        orch = self.orchestrator(escrow)
        incident = self.crash(orch)
        self.assertEqual(incident["status"], "AWAITING_APPROVAL")
        self.assertIn("Synced acme/shop@main", incident["timeline"][1]["message"])

        resolved = orch.approve(incident["incident_id"], "tester")
        self.assertEqual(resolved["resolution"]["pr_url"], "https://github.com/acme/shop/pull/1")
        pr = self.github.prs[0]
        self.assertEqual((pr["base_branch"], pr["rel_path"]), ("main", "app.py"))
        self.assertIn("if rate else 0.0", pr["patched_source"])
        self.assertIn("Bounty settled on NEAR", self.github.comments[0][1])
        workspace = Path(self.settings.projects[0].root)
        self.assertEqual((workspace / "app.py").read_text(), APP)

    def test_no_payout_when_the_pull_request_cannot_be_opened(self):
        self.connect()
        escrow = RecordingEscrow(["acme/shop"])
        orch = self.orchestrator(escrow)
        incident = self.crash(orch)
        self.github.fail_pr = "GitHub 422: Reference already exists"
        with self.assertRaisesRegex(IncidentError, "nothing was paid"):
            orch.approve(incident["incident_id"], "tester")
        self.assertNotIn("approve", [c[0] for c in escrow.calls])
        self.assertEqual(orch.store.get(incident["incident_id"])["status"], "AWAITING_APPROVAL")
        self.github.fail_pr = None
        self.assertEqual(orch.approve(incident["incident_id"], "tester")["status"], "RESOLVED")

    def test_analysis_uses_the_latest_code_on_github(self):
        self.connect()
        fixed = APP.replace("amount / rate", "(amount / rate if rate else 0.0)")
        (self.seed / "app.py").write_text(fixed)
        git(self.seed, "commit", "-q", "-am", "someone fixed it upstream")
        git(self.seed, "push", "-q", str(self.remote), "main")
        orch = self.orchestrator()
        incident = self.crash(orch)
        self.assertEqual((Path(self.settings.projects[0].root) / "app.py").read_text(), fixed)
        self.assertEqual(incident["status"], "STALE")
        self.assertIn("older than GitHub", incident["timeline"][-1]["message"])


if __name__ == "__main__":
    unittest.main()
