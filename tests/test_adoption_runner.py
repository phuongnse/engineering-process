import ctypes
import hashlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


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

    def test_main_uses_one_private_snapshot_for_install_and_apply(self):
        runner = load_runner()
        content = b"--only-binary :all:\nengineering-process==0.1.1\n"
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "requirements" / "process.txt"
            source.parent.mkdir()
            source.write_bytes(content)
            observed_snapshot: list[Path] = []

            def fake_run(argv, *, cwd):
                del cwd
                if "pip" in argv:
                    snapshot = Path(argv[-1])
                    self.assertEqual(content, snapshot.read_bytes())
                    self.assertNotEqual(source.resolve(), snapshot)
                    observed_snapshot.append(snapshot)
                    return ""
                if "adoption" in argv:
                    snapshot = Path(argv[argv.index("--requirements-lock") + 1])
                    checkout = Path(argv[argv.index("--requirements-source") + 1])
                    expected = argv[
                        argv.index("--expected-requirements-digest") + 1
                    ]
                    self.assertEqual(observed_snapshot, [snapshot])
                    self.assertEqual(source.resolve(), checkout)
                    self.assertEqual(digest, expected)
                    return json.dumps({"requirementsDigest": digest})
                return ""

            with (
                mock.patch.object(runner, "_run", side_effect=fake_run),
                mock.patch.object(runner.sys.stdout, "write"),
            ):
                self.assertEqual(
                    0,
                    runner.main(
                        [
                            "--project-root",
                            str(root),
                            "--requirements-lock",
                            "requirements/process.txt",
                        ]
                    ),
                )

    def test_main_rejects_requirements_symlink_before_running_commands(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "canonical.txt"
            target.write_text("locked\n", encoding="utf-8")
            source = root / "process.txt"
            try:
                source.symlink_to(target)
            except OSError as error:
                self.skipTest(f"symlink unavailable: {error}")

            with (
                mock.patch.object(runner, "_run") as run,
                self.assertRaisesRegex(RuntimeError, "link or reparse"),
            ):
                runner.main(
                    [
                        "--project-root",
                        str(root),
                        "--requirements-lock",
                        str(source),
                    ]
                )
            run.assert_not_called()

    def test_main_detects_checkout_mutation_between_install_and_apply(self):
        runner = load_runner()
        content = b"locked authority A\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "process.txt"
            source.write_bytes(content)
            calls = 0

            def fake_run(argv, *, cwd):
                nonlocal calls
                del cwd
                calls += 1
                if "pip" in argv:
                    self.assertEqual(content, Path(argv[-1]).read_bytes())
                    source.write_bytes(b"locked authority B\n")
                return ""

            with (
                mock.patch.object(runner, "_run", side_effect=fake_run),
                self.assertRaisesRegex(RuntimeError, "changed during adoption"),
            ):
                runner.main(
                    [
                        "--project-root",
                        str(root),
                        "--requirements-lock",
                        str(source),
                    ]
                )
            self.assertEqual(2, calls)

    def test_requirements_source_rejects_a_link_in_its_parent_chain(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inside = root / "inside"
            inside.mkdir()
            (inside / "process.txt").write_bytes(b"authority A\n")
            alias = root / "requirements"
            try:
                alias.symlink_to(inside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlink unavailable: {error}")

            with self.assertRaisesRegex(RuntimeError, "link or reparse"):
                runner._requirements_source(root, alias / "process.txt")

    def test_requirements_source_rejects_a_link_created_during_validation(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            requirements = root / "requirements"
            saved = root / "saved-requirements"
            alternate = root / "alternate"
            requirements.mkdir()
            alternate.mkdir()
            (requirements / "process.txt").write_bytes(b"authority A\n")
            (alternate / "process.txt").write_bytes(b"authority B\n")
            real_chain = runner._path_identity_chain
            calls = 0

            def swap_after_chain(chain_root, path):
                nonlocal calls
                result = real_chain(chain_root, path)
                calls += 1
                if calls == 1:
                    requirements.rename(saved)
                    try:
                        requirements.symlink_to(
                            alternate, target_is_directory=True
                        )
                    except OSError as error:
                        self.skipTest(
                            f"directory symlink unavailable: {error}"
                        )
                return result

            with (
                mock.patch.object(
                    runner,
                    "_path_identity_chain",
                    side_effect=swap_after_chain,
                ),
                self.assertRaisesRegex(RuntimeError, "link or reparse"),
            ):
                runner._requirements_source(
                    root, requirements / "process.txt"
                )

    def test_main_fails_if_parent_becomes_a_link_during_first_command(self):
        runner = load_runner()
        content = b"locked authority A\n"
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            root = Path(directory).resolve()
            requirements = root / "requirements"
            saved = root / "saved-requirements"
            outside = Path(outside_directory).resolve()
            requirements.mkdir()
            source = requirements / "process.txt"
            source.write_bytes(content)
            (outside / "process.txt").write_bytes(b"locked authority B\n")
            calls = 0

            def fake_run(argv, *, cwd):
                nonlocal calls
                del cwd
                calls += 1
                if calls == 1:
                    requirements.rename(saved)
                    try:
                        requirements.symlink_to(
                            outside, target_is_directory=True
                        )
                    except OSError as error:
                        self.skipTest(
                            f"directory symlink unavailable: {error}"
                        )
                elif "pip" in argv:
                    self.assertEqual(content, Path(argv[-1]).read_bytes())
                return ""

            with (
                mock.patch.object(runner, "_run", side_effect=fake_run),
                self.assertRaisesRegex(RuntimeError, "link or reparse"),
            ):
                runner.main(
                    [
                        "--project-root",
                        str(root),
                        "--requirements-lock",
                        "requirements/process.txt",
                    ]
                )

            self.assertEqual(2, calls)
            self.assertFalse((root / ".process" / "process.lock").exists())

    def test_path_identity_rejects_windows_reparse_attributes(self):
        runner = load_runner()
        value = SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_file_attributes=runner.FILE_ATTRIBUTE_REPARSE_POINT,
        )

        self.assertTrue(runner._is_link_or_reparse(value))

    def test_parent_swap_during_open_is_detected(self):
        runner = load_runner()
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            root = Path(directory).resolve()
            inside = root / "inside"
            saved = root / "saved"
            outside = Path(outside_directory).resolve()
            inside.mkdir()
            (inside / "process.txt").write_bytes(b"authority A\n")
            (outside / "process.txt").write_bytes(b"authority B\n")
            source = inside / "process.txt"
            real_open = runner.os.open

            def swap_then_open(path, flags, *args):
                inside.rename(saved)
                try:
                    inside.symlink_to(outside, target_is_directory=True)
                except OSError as error:
                    self.skipTest(f"directory symlink unavailable: {error}")
                return real_open(path, flags, *args)

            with (
                mock.patch.object(runner.os, "open", side_effect=swap_then_open),
                self.assertRaisesRegex(
                    RuntimeError, "changed while opening|link or reparse"
                ),
            ):
                runner._read_stable_requirements(
                    source, containment_root=root
                )


if __name__ == "__main__":
    unittest.main()
