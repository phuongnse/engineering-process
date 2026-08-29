from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from engineering_process.commands import run_check, run_profile


class CommandTests(unittest.TestCase):
    def test_output_budget_terminates_noisy_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = run_check(
                Path(directory),
                {
                    "id": "unit",
                    "run": [sys.executable, "-c", "print('x' * 100000)"],
                    "timeoutSeconds": 10,
                    "maxOutputBytes": 1024,
                },
            )
        self.assertEqual("failed", report["status"])
        self.assertTrue(report["outputExceeded"])
        self.assertLessEqual(report["stdout"]["bytes"], 1024)
        self.assertTrue(report["stdout"]["truncated"])
        self.assertRegex(report["stdout"]["sha256"], r"^sha256:[0-9a-f]{64}$")

    def test_profile_stops_at_first_failure(self) -> None:
        project = {
            "profiles": {
                "development": [
                    {
                        "id": "fail",
                        "run": [sys.executable, "-c", "raise SystemExit(7)"],
                        "timeoutSeconds": 10,
                    },
                    {
                        "id": "never",
                        "run": [sys.executable, "-c", "raise SystemExit(0)"],
                        "timeoutSeconds": 10,
                    },
                ]
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            report = run_profile(Path(directory), project, "development")
        self.assertEqual("failed", report["status"])
        self.assertEqual(["fail"], [item["id"] for item in report["checks"]])

    def test_timeout_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = run_check(
                Path(directory),
                {
                    "id": "timeout",
                    "run": [sys.executable, "-c", "import time; time.sleep(30)"],
                    "timeoutSeconds": 1,
                },
            )
        self.assertEqual("failed", report["status"])
        self.assertTrue(report["timedOut"])

    @unittest.skipIf(os.name == "nt", "POSIX process-group assertion")
    def test_surviving_descendant_is_terminated_and_fails(self) -> None:
        script = (
            "import subprocess, sys; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            "raise SystemExit(0)"
        )
        with tempfile.TemporaryDirectory() as directory:
            report = run_check(
                Path(directory),
                {
                    "id": "descendant",
                    "run": [sys.executable, "-c", script],
                    "timeoutSeconds": 10,
                },
            )
        self.assertEqual("failed", report["status"])
        self.assertTrue(report["descendantsTerminated"])

    @unittest.skipUnless(
        sys.platform.startswith("linux"), "Linux subreaper containment assertion"
    )
    def test_detached_descendant_is_terminated_and_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_path = root / "child.pid"
            child = "import time; time.sleep(30)"
            script = (
                "import pathlib, subprocess, sys; "
                f"p=subprocess.Popen([sys.executable, '-c', {child!r}], "
                "start_new_session=True, stdout=subprocess.DEVNULL, "
                "stderr=subprocess.DEVNULL); "
                "pathlib.Path(sys.argv[1]).write_text(str(p.pid), encoding='utf-8')"
            )
            report = run_check(
                root,
                {
                    "id": "detached",
                    "run": [sys.executable, "-c", script, str(pid_path)],
                    "timeoutSeconds": 10,
                },
            )
            pid = int(pid_path.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 3
            while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assertEqual("failed", report["status"])
        self.assertTrue(report["descendantsTerminated"])
        self.assertFalse(Path(f"/proc/{pid}").exists())

    @unittest.skipUnless(os.name == "nt", "Windows Job Object containment assertion")
    def test_windows_job_object_terminates_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_path = root / "child.pid"
            script = (
                "import pathlib, subprocess, sys; "
                "p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
                "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                "pathlib.Path(sys.argv[1]).write_text(str(p.pid), encoding='utf-8')"
            )
            report = run_check(
                root,
                {
                    "id": "windows-descendant",
                    "run": [sys.executable, "-c", script, str(pid_path)],
                    "timeoutSeconds": 10,
                },
            )
        self.assertEqual("failed", report["status"])
        self.assertTrue(report["descendantsTerminated"])


if __name__ == "__main__":
    unittest.main()
