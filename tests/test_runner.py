import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

from engineering_process.contracts import Check, ContractError, Project
from engineering_process.runner import run_profile


class RunnerTests(unittest.TestCase):
    def initialize_repository(self, root: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "process-test@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Process Test"],
            cwd=root,
            check=True,
        )
        (root / "tracked.txt").write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)

    def test_runs_without_shell_and_records_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = Project(
                identifier="sample",
                profiles={
                    "development": (
                        Check(
                            identifier="pass",
                            run=(sys.executable, "-c", "raise SystemExit(0)"),
                            timeout_seconds=10,
                            working_directory=".",
                        ),
                    )
                },
            )

            report = run_profile(root, project, "development")

            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["checks"][0]["status"], "passed")
            self.assertEqual(report["checks"][0]["command"][0], sys.executable)
            self.assertEqual(len(report["checks"][0]["commandSha256"]), 64)
            self.assertIsNone(report["workspaceFingerprint"])
            self.assertFalse(report["sourceChangedDuringVerification"])

    def test_reports_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = Project(
                identifier="sample",
                profiles={
                    "development": (
                        Check(
                            identifier="fail",
                            run=(sys.executable, "-c", "raise SystemExit(7)"),
                            timeout_seconds=10,
                            working_directory=".",
                        ),
                    )
                },
            )

            report = run_profile(root, project, "development")

            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["checks"][0]["exitCode"], 7)

    def test_rejects_missing_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = Project(
                identifier="sample",
                profiles={
                    "development": (
                        Check(
                            identifier="check",
                            run=(sys.executable, "-c", "pass"),
                            timeout_seconds=10,
                            working_directory="missing",
                        ),
                    )
                },
            )

            with self.assertRaisesRegex(ContractError, "does not exist"):
                run_profile(root, project, "development")

    def test_terminates_timed_out_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = Project(
                identifier="sample",
                profiles={
                    "development": (
                        Check(
                            identifier="timeout",
                            run=(
                                sys.executable,
                                "-c",
                                "import time; time.sleep(30)",
                            ),
                            timeout_seconds=1,
                            working_directory=".",
                        ),
                    )
                },
            )

            report = run_profile(root, project, "development")

            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["checks"][0]["status"], "timed-out")

    def test_invalidates_evidence_when_source_changes_during_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.initialize_repository(root)
            project = Project(
                identifier="sample",
                profiles={
                    "development": (
                        Check(
                            identifier="mutate",
                            run=(
                                sys.executable,
                                "-c",
                                "from pathlib import Path; "
                                "Path('tracked.txt').write_text('after\\n')",
                            ),
                            timeout_seconds=10,
                            working_directory=".",
                        ),
                    )
                },
            )

            report = run_profile(root, project, "development")

            self.assertEqual(report["checks"][0]["status"], "passed")
            self.assertEqual(report["status"], "failed")
            self.assertFalse(report["workingTreeDirty"])
            self.assertTrue(report["sourceChangedDuringVerification"])
            self.assertNotEqual(
                report["workspaceFingerprint"],
                report["completedWorkspaceFingerprint"],
            )
