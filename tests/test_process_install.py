import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from engineering_process.bounded_process import BoundedProcessResult
from verification import install_process_runtime as installer
from verification.install_process_runtime import (
    Attempt,
    BACKOFF_SECONDS,
    InstallError,
    PUBLIC_INDEX,
    install_process_runtime,
)


class ProcessRuntimeInstallTests(unittest.TestCase):
    class Capture:
        def __init__(self):
            self.buffer = io.BytesIO()

        def write(self, value):
            return self.buffer.write(value.encode("utf-8"))

        def flush(self):
            return None

    def setUp(self):
        self.install_stdout = self.Capture()
        self.install_stderr = self.Capture()
        stdout = patch.object(installer.sys, "stdout", self.install_stdout)
        stderr = patch.object(installer.sys, "stderr", self.install_stderr)
        stdout.start()
        stderr.start()
        self.addCleanup(stdout.stop)
        self.addCleanup(stderr.stop)

    def fixture(self, root: Path) -> Path:
        lock = root / "requirements" / "process.txt"
        lock.parent.mkdir()
        lock.write_text(
            "--only-binary :all:\n\nengineering-process==0.2.1 \\\n"
            "    --hash=sha256:cbabb56b367f5d48e5f2d0e6dd5837eca7ea46c40b47ef232d7d04e014609153\n",
            encoding="utf-8",
        )
        return lock

    def test_installs_exact_hash_lock_from_public_index_with_sanitized_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = self.fixture(root)
            calls = []

            def runner(command, cwd, environment):
                calls.append((tuple(command), cwd, environment))
                return Attempt(0, b"installed\n", b"")

            index_name = "_".join(("PIP", "INDEX", "URL"))
            extra_index_name = "_".join(("PIP", "EXTRA", "INDEX", "URL"))
            with patch.dict(
                os.environ,
                {
                    index_name: "untrusted-index-sentinel",
                    extra_index_name: "untrusted-extra-sentinel",
                    "PYTHONPATH": "/untrusted",
                },
                clear=False,
            ):
                install_process_runtime(root, lock, runner=runner)

        self.assertEqual(1, len(calls))
        command, cwd, environment = calls[0]
        self.assertEqual(root.resolve(), cwd)
        self.assertIn("--require-hashes", command)
        self.assertIn("--no-cache-dir", command)
        self.assertEqual(PUBLIC_INDEX, command[command.index("--index-url") + 1])
        self.assertEqual(str(lock.resolve()), command[-1])
        self.assertNotIn(index_name, environment)
        self.assertNotIn(extra_index_name, environment)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertEqual(os.devnull, environment["PIP_CONFIG_FILE"])
        self.assertIn(b"installed", self.install_stdout.buffer.getvalue())

    def test_exit_zero_pip_diagnostic_fails_closed_without_raw_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = self.fixture(root)
            diagnostic = b"WARNING: secret-shaped=value\n"

            with self.assertRaisesRegex(
                InstallError, "pip install emitted forbidden warning/error diagnostics"
            ) as raised:
                install_process_runtime(
                    root,
                    lock,
                    runner=lambda *_arguments: Attempt(0, b"", diagnostic),
                )

        self.assertNotIn("secret-shaped", str(raised.exception))

    def test_retries_only_exact_pinned_version_absence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = self.fixture(root)
            attempts = iter(
                (
                    Attempt(
                        1,
                        b"",
                        (
                            b"Could not find a version that satisfies the requirement "
                            b"engineering-process==0.2.1\n"
                            b"No matching distribution found for "
                            b"engineering-process==0.2.1\n"
                        ),
                    ),
                    Attempt(0, b"installed\n", b""),
                )
            )
            sleeps = []

            install_process_runtime(
                root,
                lock,
                runner=lambda *_arguments: next(attempts),
                sleeper=sleeps.append,
            )

        self.assertEqual([BACKOFF_SECONDS[0]], sleeps)
        self.assertIn(
            b"No matching distribution found",
            self.install_stderr.buffer.getvalue(),
        )

    def test_fails_immediately_for_hash_or_other_deterministic_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = self.fixture(root)
            sleeps = []

            with self.assertRaisesRegex(InstallError, "non-retryable"):
                install_process_runtime(
                    root,
                    lock,
                    runner=lambda *_arguments: Attempt(
                        1, b"", b"THESE PACKAGES DO NOT MATCH THE HASHES\n"
                    ),
                    sleeper=sleeps.append,
                )

        self.assertEqual([], sleeps)
        self.assertIn(
            b"PACKAGES DO NOT MATCH THE HASHES",
            self.install_stderr.buffer.getvalue(),
        )

    def test_bounds_timeout_output_and_eventual_absence(self):
        scenarios = (
            (Attempt(-1, b"", b"", timed_out=True), "exceeded 300 seconds"),
            (Attempt(1, b"", b"", output_exceeded=True), "output exceeded"),
        )
        for attempt, expected in scenarios:
            with self.subTest(expected), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                lock = self.fixture(root)
                with self.assertRaisesRegex(InstallError, expected):
                    install_process_runtime(
                        root,
                        lock,
                        runner=lambda *_arguments, value=attempt: value,
                        sleeper=lambda _delay: None,
                    )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = self.fixture(root)
            transient = Attempt(
                1,
                b"",
                (
                    b"Could not find a version that satisfies the requirement "
                    b"engineering-process==0.2.1\n"
                    b"No matching distribution found for engineering-process==0.2.1\n"
                ),
            )
            sleeps = []
            with self.assertRaisesRegex(InstallError, "did not become visible"):
                install_process_runtime(
                    root,
                    lock,
                    runner=lambda *_arguments: transient,
                    sleeper=sleeps.append,
                )

        self.assertEqual(list(BACKOFF_SECONDS), sleeps)

    def test_rejects_ambiguous_non_binary_or_symlinked_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = self.fixture(root)
            lock.write_text(
                "engineering-process==0.2.1\nengineering-process==0.2.1\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(InstallError, "exactly one"):
                install_process_runtime(
                    root,
                    lock,
                    runner=lambda *_arguments: Attempt(0, b"", b""),
                )

        if os.name != "nt":
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "target.txt"
                target.write_text(
                    "--only-binary :all:\nengineering-process==0.2.1\n",
                    encoding="utf-8",
                )
                link = root / "requirements" / "process.txt"
                link.parent.mkdir()
                link.symlink_to(target)
                with self.assertRaisesRegex(InstallError, "symlinks"):
                    install_process_runtime(
                        root,
                        link,
                        runner=lambda *_arguments: Attempt(0, b"", b""),
                    )

    def test_real_runner_bounds_output_and_terminates_on_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(installer, "MAX_OUTPUT_BYTES", 1024):
                attempt = installer._run_attempt(
                    [
                        sys.executable,
                        "-c",
                        "import sys; sys.stdout.buffer.write(b'x' * 100000); sys.stdout.flush()",
                    ],
                    root,
                    os.environ.copy(),
                )
            self.assertTrue(attempt.output_exceeded)
            self.assertLessEqual(len(attempt.stdout) + len(attempt.stderr), 1024)

            with (
                patch.object(installer, "ATTEMPT_TIMEOUT_SECONDS", 0.1),
                patch.object(installer, "TERMINATION_TIMEOUT_SECONDS", 1.0),
            ):
                started = installer.time.monotonic()
                attempt = installer._run_attempt(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    root,
                    os.environ.copy(),
                )
                elapsed = installer.time.monotonic() - started
            self.assertTrue(attempt.timed_out)
            self.assertLess(elapsed, 3.0)

    def test_windows_runner_maps_the_shared_bounded_result_exactly(self):
        result = BoundedProcessResult(
            returncode=7,
            stdout=b"stdout",
            stderr=b"stderr",
            timed_out=False,
            output_exceeded=False,
            descendants_found=True,
            input_error=False,
            cleanup_error=None,
        )
        with tempfile.TemporaryDirectory() as directory:
            working_directory = Path(directory)
            with (
                patch.object(installer.os, "name", "nt"),
                patch.object(
                    installer, "run_bounded_process", return_value=result
                ) as bounded,
            ):
                attempt = installer._run_attempt(
                    [r"C:\Python\python.exe", "-m", "pip"],
                    working_directory,
                    {"PATH": r"C:\Python"},
                )

        self.assertEqual(7, attempt.returncode)
        self.assertEqual(b"stdout", attempt.stdout)
        self.assertEqual(b"stderr", attempt.stderr)
        self.assertTrue(attempt.descendants_terminated)
        bounded.assert_called_once_with(
            [r"C:\Python\python.exe", "-m", "pip"],
            working_directory=working_directory,
            environment={"PATH": r"C:\Python"},
            timeout_seconds=installer.ATTEMPT_TIMEOUT_SECONDS,
            max_stream_bytes=installer.MAX_OUTPUT_BYTES,
            max_total_bytes=installer.MAX_OUTPUT_BYTES,
        )

    def test_windows_runner_preserves_normal_exit_125_and_fails_on_cleanup(self):
        normal = BoundedProcessResult(
            returncode=125,
            stdout=b"",
            stderr=b"",
            timed_out=False,
            output_exceeded=False,
            descendants_found=False,
            input_error=False,
            cleanup_error=None,
        )
        failed = BoundedProcessResult(
            returncode=None,
            stdout=b"",
            stderr=b"",
            timed_out=False,
            output_exceeded=False,
            descendants_found=False,
            input_error=False,
            cleanup_error="status unavailable",
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            installer.os, "name", "nt"
        ):
            with patch.object(
                installer, "run_bounded_process", return_value=normal
            ):
                attempt = installer._run_attempt(
                    [r"C:\Python\python.exe"], Path(directory), {}
                )
            with (
                patch.object(
                    installer, "run_bounded_process", return_value=failed
                ),
                self.assertRaisesRegex(InstallError, "status unavailable"),
            ):
                installer._run_attempt(
                    [r"C:\Python\python.exe"], Path(directory), {}
                )

        self.assertEqual(125, attempt.returncode)
        self.assertFalse(attempt.descendants_terminated)

    @unittest.skipIf(os.name == "nt", "Windows descendants are covered by Job Object tests")
    def test_real_runner_terminates_descendants_left_by_successful_root(self):
        with tempfile.TemporaryDirectory() as directory:
            attempt = installer._run_attempt(
                [
                    sys.executable,
                    "-c",
                    (
                        "import subprocess, sys; "
                        "subprocess.Popen([sys.executable, '-c', "
                        "'import time; time.sleep(30)'])"
                    ),
                ],
                Path(directory),
                os.environ.copy(),
            )
        self.assertTrue(attempt.descendants_terminated)


if __name__ == "__main__":
    unittest.main()
