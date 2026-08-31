from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from engineering_process import _supervisor_posix as posix
from engineering_process.commands import run_check


class PosixSupervisorTests(unittest.TestCase):
    def test_ps_snapshot_failures_are_sanitized_and_bounded(self) -> None:
        cases = (
            (
                subprocess.TimeoutExpired(["ps"], posix.PROCESS_TABLE_TIMEOUT_SECONDS),
                "process table snapshot timed out after 10 seconds",
            ),
            (OSError("sensitive host detail"), "process table snapshot could not start"),
        )
        for failure, expected in cases:
            with self.subTest(expected=expected), patch.object(
                posix.subprocess, "run", side_effect=failure
            ) as run:
                table, error = posix._ps_process_table()
            self.assertEqual({}, table)
            self.assertEqual(expected, error)
            self.assertEqual(
                posix.PROCESS_TABLE_TIMEOUT_SECONDS,
                run.call_args.kwargs["timeout"],
            )

        result = SimpleNamespace(returncode=7, stdout="")
        with patch.object(posix.subprocess, "run", return_value=result):
            table, error = posix._ps_process_table()
        self.assertEqual({}, table)
        self.assertEqual("process table snapshot exited with status 7", error)

        result = SimpleNamespace(returncode=0, stdout="not-a-process-table\n")
        with patch.object(posix.subprocess, "run", return_value=result):
            table, error = posix._ps_process_table()
        self.assertEqual({}, table)
        self.assertEqual("process table snapshot was incomplete", error)

    def test_routine_observation_spaces_process_table_snapshots(self) -> None:
        supervisor = posix.PosixProcessSupervisor()
        process = SimpleNamespace(pid=12345)
        with (
            patch.object(posix, "_process_table", return_value=({}, None)) as snapshot,
            patch.object(posix.time, "sleep") as pause,
        ):
            supervisor.observe(process)
            supervisor.observe(process)
        self.assertEqual(2, snapshot.call_count)
        self.assertEqual(2, pause.call_count)
        self.assertTrue(all(item.args == (0.04,) for item in pause.call_args_list))

    def test_snapshot_failure_makes_detached_cleanup_fail_closed(self) -> None:
        supervisor = posix.PosixProcessSupervisor()
        process = SimpleNamespace(pid=12345)
        supervisor._known_descendants[process.pid] = set()
        supervisor._run_ids[process.pid] = "bounded-test-run"
        with patch.object(
            posix,
            "_process_table",
            return_value=({}, "process table snapshot timed out after 10 seconds"),
        ):
            outcome = supervisor._terminate_detached(process, grace_seconds=0)
        self.assertFalse(outcome.bounded)
        self.assertFalse(outcome.descendants_found)
        self.assertEqual(
            "process table snapshot timed out after 10 seconds", outcome.error
        )

    @unittest.skipIf(os.name == "nt", "POSIX cleanup result assertion")
    def test_snapshot_failure_returns_a_failed_check_instead_of_escaping(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            posix,
            "_process_table",
            return_value=({}, "process table snapshot timed out after 10 seconds"),
        ):
            report = run_check(
                Path(directory),
                {
                    "id": "snapshot-failure",
                    "run": [sys.executable, "-c", "raise SystemExit(0)"],
                    "timeoutSeconds": 10,
                },
            )
        self.assertEqual("failed", report["status"])
        self.assertTrue(report["streamFailed"])
        self.assertFalse(report["timedOut"])


if __name__ == "__main__":
    unittest.main()
