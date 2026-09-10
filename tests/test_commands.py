from __future__ import annotations

import ctypes
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest
import venv
from unittest.mock import Mock, patch

from engineering_process.commands import _child_environment, run_check, run_profile
from engineering_process.contracts import ProcessError
from engineering_process.supervision import CleanupOutcome, process_supervisor


def windows_process_is_running(process_id: int) -> bool:
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_failed = 0xFFFFFFFF
    wait_timeout = 0x00000102
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(synchronize, False, process_id)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: the PID no longer exists.
            return False
        raise ctypes.WinError(error)
    try:
        result = kernel32.WaitForSingleObject(handle, 0)
        if result == wait_failed:
            raise ctypes.WinError(ctypes.get_last_error())
        return result == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


class CommandTests(unittest.TestCase):
    def test_child_path_prefers_the_process_runtime_and_preserves_caller_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / ("python.exe" if os.name == "nt" else "python")
            inherited = os.pathsep.join(("caller-one", "caller-two"))
            with patch.dict(
                os.environ,
                {
                    "PATH": inherited,
                    "PYTHONHOME": "blocked-home",
                    "PYTHONPATH": "blocked-path",
                    "SERVICE_TOKEN": "blocked-secret",
                },
                clear=True,
            ), patch(
                "engineering_process.commands.sys.executable", str(executable)
            ):
                environment = _child_environment()

        self.assertEqual(
            [str(executable.absolute().parent), "caller-one", "caller-two"],
            environment["PATH"].split(os.pathsep),
        )
        self.assertNotIn("PYTHONHOME", environment)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("SERVICE_TOKEN", environment)

    def test_child_path_omits_an_empty_inherited_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"PATH": ""}, clear=True
        ), patch(
            "engineering_process.commands.sys.executable",
            str(Path(directory) / "python"),
        ):
            environment = _child_environment()

        self.assertNotIn("", environment["PATH"].split(os.pathsep))

    def test_bare_runtime_command_uses_the_process_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = Path(directory) / "runtime"
            venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(environment)
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            purelib = subprocess.run(
                [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                capture_output=True, text=True, check=True, timeout=30,
            ).stdout.strip()
            (Path(purelib) / "process_runtime_fixture.py").write_text("VALUE = 'environment-only'\n", encoding="utf-8")
            child = (
                "from pathlib import Path; import sys, process_runtime_fixture as fixture; "
                f"assert Path(sys.prefix).resolve() == Path({str(environment)!r}).resolve(); "
                "assert fixture.VALUE == 'environment-only'; "
                "assert Path(fixture.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())"
            )
            driver = (
                "import json, os, site, sys; from pathlib import Path; "
                f"site.addsitedir({sysconfig.get_path('purelib')!r}); "
                f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r}); "
                "from engineering_process.commands import run_check; os.environ['PATH'] = ''; "
                f"check = {{'id': 'runtime', 'run': ['python', '-c', {child!r}], 'timeoutSeconds': 10}}; "
                f"report = run_check(Path({directory!r}), check); "
                "print(json.dumps({'report': report, 'argv0': check['run'][0]}))"
            )
            result = subprocess.run(
                [str(python), "-c", driver], capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            observed = json.loads(result.stdout)
            self.assertEqual("passed", observed["report"]["status"], observed)
            self.assertEqual("python", observed["argv0"])

    def test_runtime_path_preserves_explicit_dot_relative_commands(self) -> None:
        name = "python.exe" if os.name == "nt" else "python"
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            runtime = Path(directory) / "runtime"
            for location in (project, runtime):
                location.mkdir()
                executable = location / name
                executable.write_bytes(b"native executable fixture")
                executable.chmod(0o755)
            before = {"PATH": str(project)}
            with patch.dict(os.environ, before), patch("engineering_process.commands.sys.executable", str(runtime / name)):
                after = _child_environment()
            supervisor = process_supervisor()
            commands = [name, "./" + name, str(project / name)]
            if os.name == "nt":
                commands.append(".\\" + name)
            for command in commands:
                with self.subTest(command=command):
                    previous = supervisor.resolve_application(command, working_directory=project, environment=before)
                    current = supervisor.resolve_application(command, working_directory=project, environment=after)
                    self.assertEqual((project / name).resolve(), previous.resolve())
                    expected = runtime if command == name else project
                    self.assertEqual((expected / name).resolve(), current.resolve())

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
        self.assertEqual({"kind": "profile"}, report["scope"])
        self.assertEqual(
            {
                "kind": "selective-check-reproduction",
                "profile": "development",
                "check": "fail",
                "position": 1,
                "command": [
                    "processctl",
                    "verify",
                    "--profile",
                    "development",
                    "--check-position",
                    "1",
                ],
            },
            report["diagnostic"],
        )

    def test_profile_failure_descriptor_excludes_child_details(self) -> None:
        secret = "TOPSECRET-DIAGNOSTIC-VALUE"
        project = {
            "profiles": {
                "rust": [
                    {
                        "id": "format",
                        "run": [sys.executable, "-c", "raise SystemExit(0)"],
                        "timeoutSeconds": 10,
                    },
                    {
                        "id": "rust-tests",
                        "run": [
                            sys.executable,
                            "-c",
                            f"import sys; print({secret!r}); print({secret!r}, file=sys.stderr); raise SystemExit(101)",
                        ],
                        "timeoutSeconds": 10,
                    },
                ]
            }
        }
        with tempfile.TemporaryDirectory(prefix="secret-working-directory-") as directory:
            report = run_profile(Path(directory), project, "rust")
        self.assertEqual(["format", "rust-tests"], [item["id"] for item in report["checks"]])
        self.assertEqual(101, report["checks"][-1]["exitCode"])
        self.assertEqual("rust-tests", report["diagnostic"]["check"])
        self.assertEqual(2, report["diagnostic"]["position"])
        rendered = repr(report)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(sys.executable, rendered)
        self.assertNotIn("secret-working-directory", rendered)

    def test_selective_reproduction_runs_only_the_named_check(self) -> None:
        project = {
            "profiles": {
                "rust": [
                    {
                        "id": "would-fail",
                        "run": [sys.executable, "-c", "raise SystemExit(7)"],
                        "timeoutSeconds": 10,
                    },
                    {
                        "id": "rust-tests",
                        "run": [sys.executable, "-c", "raise SystemExit(0)"],
                        "timeoutSeconds": 10,
                    },
                ]
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            report = run_profile(Path(directory), project, "rust", check_position=2)
        self.assertEqual("passed", report["status"])
        self.assertEqual(
            {"kind": "check", "check": "rust-tests", "position": 2},
            report["scope"],
        )
        self.assertEqual(["rust-tests"], [item["id"] for item in report["checks"]])
        self.assertNotIn("diagnostic", report)

    def test_duplicate_ids_reproduce_one_authoritative_position(self) -> None:
        project = {
            "profiles": {
                "rust": [
                    {
                        "id": "rust-tests",
                        "run": [sys.executable, "-c", "raise SystemExit(0)"],
                        "timeoutSeconds": 10,
                    },
                    {
                        "id": "rust-tests",
                        "run": [sys.executable, "-c", "raise SystemExit(101)"],
                        "timeoutSeconds": 10,
                    }
                ]
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            report = run_profile(Path(directory), project, "rust")
            reproduced = run_profile(
                Path(directory), project, "rust", check_position=2
            )
        self.assertEqual(2, report["diagnostic"]["position"])
        self.assertEqual(["rust-tests"], [item["id"] for item in reproduced["checks"]])
        self.assertEqual(101, reproduced["checks"][0]["exitCode"])

    def test_selective_reproduction_rejects_unknown_position(self) -> None:
        project = {
            "profiles": {
                "rust": [
                    {
                        "id": "rust-tests",
                        "run": [sys.executable, "-c", "raise SystemExit(0)"],
                        "timeoutSeconds": 10,
                    }
                ]
            }
        }
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(
            ProcessError, "profile rust has no check at position: 2"
        ):
            run_profile(Path(directory), project, "rust", check_position=2)

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

    def test_unbounded_cleanup_fails_without_an_adapter_error(self) -> None:
        process = Mock(stdout=io.BytesIO(), stderr=io.BytesIO(), returncode=0)
        process.poll.return_value = 0
        supervisor = Mock()
        supervisor.spawn.return_value = process
        supervisor.finalize.return_value = CleanupOutcome(bounded=False)
        with tempfile.TemporaryDirectory() as directory, patch(
            "engineering_process.commands.process_supervisor",
            return_value=supervisor,
        ):
            report = run_check(
                Path(directory),
                {
                    "id": "unbounded-cleanup",
                    "run": [sys.executable, "-c", "raise SystemExit(0)"],
                    "timeoutSeconds": 10,
                },
            )
        self.assertEqual("failed", report["status"])
        self.assertTrue(report["streamFailed"])

    @unittest.skipUnless(
        sys.platform.startswith("linux"), "Linux subreaper ownership assertion"
    )
    def test_unrelated_child_is_not_claimed_by_supervision(self) -> None:
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                report = run_check(
                    Path(directory),
                    {
                        "id": "owned-command",
                        "run": [sys.executable, "-c", "raise SystemExit(0)"],
                        "timeoutSeconds": 10,
                    },
                )
            self.assertEqual("passed", report["status"])
            self.assertIsNone(unrelated.poll())
        finally:
            if unrelated.poll() is None:
                unrelated.terminate()
            unrelated.wait(timeout=3)

    @unittest.skipIf(os.name == "nt", "POSIX process-group assertion")
    def test_surviving_descendant_is_terminated_after_success(self) -> None:
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
        self.assertEqual("passed", report["status"])
        self.assertTrue(report["descendantsTerminated"])

    @unittest.skipUnless(
        sys.platform.startswith("linux"), "Linux subreaper reaping assertion"
    )
    def test_exited_descendant_is_reaped_and_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_path = root / "child.pid"
            child = "import time; time.sleep(0.01)"
            script = (
                "import pathlib, subprocess, sys, time; "
                f"p=subprocess.Popen([sys.executable, '-c', {child!r}]); "
                "pathlib.Path(sys.argv[1]).write_text(str(p.pid), encoding='utf-8'); "
                "time.sleep(0.05)"
            )
            report = run_check(
                root,
                {
                    "id": "naturally-drained-descendant",
                    "run": [sys.executable, "-c", script, str(pid_path)],
                    "timeoutSeconds": 10,
                },
            )
            pid = int(pid_path.read_text(encoding="utf-8"))
        self.assertEqual("passed", report["status"])
        self.assertFalse(report["descendantsTerminated"])
        self.assertFalse(report["streamFailed"])
        self.assertFalse(Path(f"/proc/{pid}").exists())

    @unittest.skipUnless(
        sys.platform.startswith("linux"), "Linux subreaper containment assertion"
    )
    def test_detached_descendant_is_terminated_after_success(self) -> None:
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
        self.assertEqual("passed", report["status"])
        self.assertTrue(report["descendantsTerminated"])
        self.assertFalse(Path(f"/proc/{pid}").exists())

    @unittest.skipUnless(os.name == "nt", "Windows Job Object containment assertion")
    def test_windows_job_object_terminates_descendants_after_success(self) -> None:
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
            pid = int(pid_path.read_text(encoding="utf-8"))
        self.assertEqual("passed", report["status"])
        self.assertTrue(report["descendantsTerminated"])
        self.assertFalse(windows_process_is_running(pid))


if __name__ == "__main__":
    unittest.main()
