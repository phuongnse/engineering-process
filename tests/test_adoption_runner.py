import importlib.util
import ctypes
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path


PROCESS_ROOT = Path(__file__).resolve().parent.parent


def load_runner():
    path = PROCESS_ROOT / "templates" / "adopt-process.py"
    spec = importlib.util.spec_from_file_location("managed_adoption_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load managed adoption runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AdoptionRunnerTests(unittest.TestCase):
    def process_is_running(self, pid: int) -> bool:
        if os.name == "nt":
            process_query_limited_information = 0x1000
            still_active = 259
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
            handle = kernel32.OpenProcess(
                process_query_limited_information, False, pid
            )
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == still_active
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

    def descendant_command(
        self,
        pid_path: Path,
        *,
        exit_code: int = 0,
        retain_output: bool = False,
        parent_sleep: bool = False,
    ) -> list[str]:
        output = (
            ""
            if retain_output
            else ", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL"
        )
        script = (
            "import pathlib, subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']"
            f"{output}); "
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8'); "
            + ("time.sleep(30); " if parent_sleep else "")
            + f"raise SystemExit({exit_code})"
        )
        return [sys.executable, "-c", script, str(pid_path)]

    def test_command_output_is_drained_and_bounded(self):
        runner = load_runner()
        runner.MAX_CAPTURE_BYTES = 64
        with tempfile.TemporaryDirectory() as directory:
            output = runner._run(
                [sys.executable, "-c", "print('x' * 10000)"],
                cwd=Path(directory),
            )

        self.assertLess(len(output), 256)
        self.assertIn("output truncated: 10001 bytes", output)
        self.assertRegex(output, r"sha256:[0-9a-f]{64}")

    def test_command_timeout_terminates_the_process_group(self):
        runner = load_runner()
        runner.COMMAND_TIMEOUT_SECONDS = 0.01
        runner.TERMINATION_TIMEOUT_SECONDS = 2
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                runner._run(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    cwd=Path(directory),
                )

    def test_successful_parent_cannot_leave_detached_output_descendant(self):
        runner = load_runner()
        runner.TERMINATION_TIMEOUT_SECONDS = 2
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_path = root / "descendant.pid"
            with self.assertRaisesRegex(RuntimeError, "descendant processes"):
                runner._run(self.descendant_command(pid_path), cwd=root)
            self.assert_process_stopped(pid_path)

    def test_failed_parent_cannot_leave_descendant(self):
        runner = load_runner()
        runner.TERMINATION_TIMEOUT_SECONDS = 2
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_path = root / "descendant.pid"
            with self.assertRaises(RuntimeError):
                runner._run(
                    self.descendant_command(pid_path, exit_code=3),
                    cwd=root,
                )
            self.assert_process_stopped(pid_path)

    def test_timeout_cannot_leave_descendant(self):
        runner = load_runner()
        runner.COMMAND_TIMEOUT_SECONDS = 0.2
        runner.TERMINATION_TIMEOUT_SECONDS = 2
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_path = root / "descendant.pid"
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                runner._run(
                    self.descendant_command(pid_path, parent_sleep=True),
                    cwd=root,
                )
            self.assert_process_stopped(pid_path)

    def test_parent_exit_with_retained_pipe_cannot_leave_descendant(self):
        runner = load_runner()
        runner.TERMINATION_TIMEOUT_SECONDS = 2
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_path = root / "descendant.pid"
            with self.assertRaisesRegex(RuntimeError, "descendant processes"):
                runner._run(
                    self.descendant_command(pid_path, retain_output=True),
                    cwd=root,
                )
            self.assert_process_stopped(pid_path)

    def test_child_environment_does_not_forward_credentials_or_python_paths(self):
        runner = load_runner()
        previous = {
            key: os.environ.get(key)
            for key in ("GH_TOKEN", "PIP_INDEX_URL", "PYTHONPATH")
        }
        try:
            os.environ["GH_TOKEN"] = "secret"
            os.environ["PIP_INDEX_URL"] = "https://secret@example.invalid/simple"
            os.environ["PYTHONPATH"] = "/untrusted"
            environment = runner._child_environment()
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertNotIn("GH_TOKEN", environment)
        self.assertNotIn("PIP_INDEX_URL", environment)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertEqual(os.devnull, environment["PIP_CONFIG_FILE"])


if __name__ == "__main__":
    unittest.main()
