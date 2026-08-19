import sys
import subprocess
import tempfile
import unittest
import os
import py_compile
from pathlib import Path
from unittest.mock import patch

from engineering_process.contracts import (
    Check,
    ContractError,
    ImpactComponent,
    Project,
    ProjectImpact,
)
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
            self.assertEqual(report["impact"]["mode"], "full-profile")
            self.assertIsNone(report["workspaceFingerprint"])
            self.assertFalse(report["sourceChangedDuringVerification"])

    def test_empty_affected_selection_is_passing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.initialize_repository(root)
            project = Project(
                identifier="sample",
                profiles={
                    "development": (
                        Check(
                            identifier="source",
                            run=(sys.executable, "-c", "raise SystemExit(99)"),
                            timeout_seconds=10,
                            working_directory=".",
                            components=("source",),
                        ),
                    )
                },
                impact=ProjectImpact(
                    base_refs=("HEAD",),
                    unmatched_paths="all-scoped-checks",
                    components={
                        "source": ImpactComponent(
                            identifier="source",
                            paths=("src/**",),
                            affects=(),
                        )
                    },
                ),
            )

            report = run_profile(root, project, "development", base_ref="HEAD")

            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["checks"], [])
            self.assertEqual(report["impact"]["selectedCheckIds"], [])
            self.assertEqual(report["impact"]["skippedCheckIds"], ["source"])

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

    def test_ignored_python_bytecode_cannot_override_the_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.initialize_repository(root)
            source = root / "victim.py"
            source.write_text("VALUE = 'safe'\n", encoding="utf-8")
            (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "victim.py", ".gitignore"], cwd=root, check=True
            )
            subprocess.run(["git", "commit", "-qm", "add module"], cwd=root, check=True)

            source.write_text("VALUE = 'evil'\n", encoding="utf-8")
            fixed_mtime = 1_700_000_000
            os.utime(source, (fixed_mtime, fixed_mtime))
            py_compile.compile(str(source), doraise=True)
            source.write_text("VALUE = 'safe'\n", encoding="utf-8")
            os.utime(source, (fixed_mtime, fixed_mtime))
            self.assertFalse(
                subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=root,
                    check=True,
                    capture_output=True,
                ).stdout
            )
            project = Project(
                identifier="sample",
                profiles={
                    "development": (
                        Check(
                            "attested-source",
                            (
                                sys.executable,
                                "-c",
                                "import victim; assert victim.VALUE == 'safe'",
                            ),
                            10,
                            ".",
                        ),
                    )
                },
            )

            report = run_profile(root, project, "development")

            self.assertEqual("passed", report["status"])
            self.assertFalse(report["workingTreeDirty"])

    def test_ignored_sourceless_bytecode_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.initialize_repository(root)
            (root / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", ".gitignore"], cwd=root, check=True
            )
            subprocess.run(
                ["git", "commit", "-qm", "ignore bytecode"], cwd=root, check=True
            )
            source = root / "unreviewed.py"
            source.write_text("VALUE = 'evil'\n", encoding="utf-8")
            py_compile.compile(
                str(source), cfile=str(root / "unreviewed.pyc"), doraise=True
            )
            source.unlink()
            project = Project(
                identifier="sample",
                profiles={
                    "development": (
                        Check(
                            "attested-source",
                            (
                                sys.executable,
                                "-c",
                                "import unreviewed; assert unreviewed.VALUE == 'evil'",
                            ),
                            10,
                            ".",
                        ),
                    )
                },
            )

            with self.assertRaisesRegex(
                ContractError, "ignored sourceless Python bytecode"
            ):
                run_profile(root, project, "development")

    def test_each_check_receives_fresh_impact_bytes_and_tampering_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.initialize_repository(root)
            tamper = (
                "import os, pathlib, stat; "
                "p=pathlib.Path(os.environ['ENGINEERING_PROCESS_IMPACT_FILE']); "
                "p.chmod(stat.S_IRUSR | stat.S_IWUSR); p.write_text('{}')"
            )
            verify = (
                "import json, os; "
                "d=json.load(open(os.environ['ENGINEERING_PROCESS_IMPACT_FILE'], encoding='utf-8')); "
                "assert d['mode'] == 'full-profile'"
            )
            project = Project(
                identifier="sample",
                profiles={
                    "development": (
                        Check("tamper", (sys.executable, "-c", tamper), 10, "."),
                        Check("verify", (sys.executable, "-c", verify), 10, "."),
                    )
                },
            )

            report = run_profile(root, project, "development")

            self.assertEqual("failed", report["checks"][0]["impactIntegrity"])
            self.assertEqual("failed", report["checks"][0]["status"])
            self.assertEqual("verified", report["checks"][1]["impactIntegrity"])
            self.assertEqual("passed", report["checks"][1]["status"])

    def test_workspace_fingerprint_rejects_untracked_file_over_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.initialize_repository(root)
            (root / "large.bin").write_bytes(b"xx")
            project = Project(identifier="sample", profiles={"development": (
                Check("pass", (sys.executable, "-c", "pass"), 10, "."),
            )})

            with (
                patch("engineering_process.runner.MAX_UNTRACKED_FILE_BYTES", 1),
                self.assertRaisesRegex(ContractError, "untracked file exceeds"),
            ):
                run_profile(root, project, "development")

    def test_workspace_fingerprint_rejects_untracked_path_count_over_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.initialize_repository(root)
            (root / "one.txt").write_text("1", encoding="utf-8")
            (root / "two.txt").write_text("2", encoding="utf-8")
            project = Project(identifier="sample", profiles={"development": (
                Check("pass", (sys.executable, "-c", "pass"), 10, "."),
            )})

            with (
                patch("engineering_process.runner.MAX_SOURCE_PATHS", 1),
                self.assertRaisesRegex(ContractError, "path count exceeds"),
            ):
                run_profile(root, project, "development")
