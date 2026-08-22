import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from verification.install_process_runtime import (
    Attempt,
    BACKOFF_SECONDS,
    InstallError,
    PUBLIC_INDEX,
    install_process_runtime,
)


class ProcessRuntimeInstallTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
