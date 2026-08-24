import ctypes
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from engineering_process.bounded_process import run_bounded_process
from engineering_process.supervision import CleanupOutcome


class BoundedProcessTests(unittest.TestCase):
    def process_is_running(self, pid: int) -> bool:
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                ctypes.c_ulong,
                ctypes.c_int,
                ctypes.c_ulong,
            ]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_ulong),
            ]
            kernel32.GetExitCodeProcess.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(exit_code)
                ):
                    return False
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        status = Path(f"/proc/{pid}/stat")
        if status.is_file():
            try:
                return status.read_text(encoding="utf-8").split()[2] != "Z"
            except (OSError, IndexError):
                pass
        return True

    def assert_process_stopped(self, pid_path: Path) -> None:
        pid = int(pid_path.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 5
        while self.process_is_running(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.process_is_running(pid), f"descendant {pid} survived")

    def descendant_command(self, pid_path: Path, *, parent_waits: bool) -> list[str]:
        child = (
            "import os, pathlib, sys, time; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
            "time.sleep(30)"
        )
        parent = (
            "import pathlib, subprocess, sys, time\n"
            "marker = pathlib.Path(sys.argv[2])\n"
            "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])\n"
            "deadline = time.monotonic() + 5\n"
            "while not marker.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            + ("time.sleep(30)\n" if parent_waits else "raise SystemExit(0)\n")
        )
        return [sys.executable, "-c", parent, child, str(pid_path)]

    def test_streaming_output_limit_terminates_infinite_child_in_flight(self):
        with tempfile.TemporaryDirectory() as directory:
            started = time.monotonic()
            result = run_bounded_process(
                [
                    sys.executable,
                    "-c",
                    "import sys\nwhile True:\n"
                    "    sys.stderr.write('x' * 65536)\n"
                    "    sys.stderr.flush()\n",
                ],
                working_directory=Path(directory),
                environment=os.environ.copy(),
                timeout_seconds=30,
                max_stream_bytes=64_000,
                max_total_bytes=64_000,
            )
            elapsed = time.monotonic() - started

        self.assertTrue(result.output_exceeded)
        self.assertLessEqual(len(result.stderr), 64_000)
        self.assertLess(elapsed, 5)
        self.assertIsNotNone(result.returncode)

    def test_timeout_terminates_root_and_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_path = root / "descendant.pid"
            started = time.monotonic()
            result = run_bounded_process(
                self.descendant_command(pid_path, parent_waits=True),
                working_directory=root,
                environment=os.environ.copy(),
                timeout_seconds=1,
                max_stream_bytes=64_000,
                max_total_bytes=64_000,
            )
            elapsed = time.monotonic() - started

            self.assertTrue(result.timed_out)
            self.assertLess(elapsed, 5)
            self.assertTrue(pid_path.is_file())
            self.assert_process_stopped(pid_path)

    def test_successful_root_cannot_leave_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_path = root / "descendant.pid"
            result = run_bounded_process(
                self.descendant_command(pid_path, parent_waits=False),
                working_directory=root,
                environment=os.environ.copy(),
                timeout_seconds=10,
                max_stream_bytes=64_000,
                max_total_bytes=64_000,
            )

            self.assertTrue(result.descendants_found)
            self.assertTrue(pid_path.is_file())
            self.assert_process_stopped(pid_path)

    def test_interrupt_terminates_process_tree_before_propagating(self):
        process = mock.Mock()
        process.stdout = io.BytesIO(b"")
        process.stderr = io.BytesIO(b"")
        process.poll.return_value = None
        process.wait.side_effect = KeyboardInterrupt
        supervisor = mock.Mock()
        supervisor.spawn.return_value = process
        supervisor.terminate.return_value = CleanupOutcome(bounded=True)

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                "engineering_process.bounded_process.process_supervisor",
                return_value=supervisor,
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            run_bounded_process(
                ["tool"],
                working_directory=Path(directory),
                environment={},
                timeout_seconds=30,
                max_stream_bytes=64_000,
                max_total_bytes=64_000,
            )

        supervisor.terminate.assert_called_once_with(
            process, grace_seconds=1.0
        )

    def test_input_failure_is_reported_for_successful_root(self):
        class BrokenInput:
            def write(self, _content):
                raise BrokenPipeError("closed")

            def close(self):
                return None

        process = mock.Mock()
        process.stdin = BrokenInput()
        process.stdout = io.BytesIO(b"")
        process.stderr = io.BytesIO(b"")
        process.poll.side_effect = [None, 0, 0]
        process.wait.return_value = 0
        process.returncode = 0
        supervisor = mock.Mock()
        supervisor.spawn.return_value = process
        supervisor.finalize.return_value = CleanupOutcome(bounded=True)

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                "engineering_process.bounded_process.process_supervisor",
                return_value=supervisor,
            ),
        ):
            result = run_bounded_process(
                ["tool"],
                working_directory=Path(directory),
                environment={},
                timeout_seconds=30,
                max_stream_bytes=64_000,
                max_total_bytes=64_000,
                input_bytes=b"payload",
            )

        self.assertTrue(result.input_error)

    def test_bounded_input_is_delivered_without_shell(self):
        payload = b"bounded-input"
        with tempfile.TemporaryDirectory() as directory:
            result = run_bounded_process(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
                ],
                working_directory=Path(directory),
                environment=os.environ.copy(),
                timeout_seconds=30,
                max_stream_bytes=64_000,
                max_total_bytes=64_000,
                input_bytes=payload,
            )

        self.assertEqual(0, result.returncode)
        self.assertEqual(payload, result.stdout)
        self.assertFalse(result.input_error)
