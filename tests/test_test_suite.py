import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from verification.run_test_suite import configure_test_git_environment


class TestSuiteRunnerTests(unittest.TestCase):
    def test_appends_deterministic_fixture_git_config(self):
        environment = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.autocrlf",
            "GIT_CONFIG_VALUE_0": "true",
        }

        configure_test_git_environment(environment)

        self.assertEqual("3", environment["GIT_CONFIG_COUNT"])
        self.assertEqual("core.autocrlf", environment["GIT_CONFIG_KEY_1"])
        self.assertEqual("false", environment["GIT_CONFIG_VALUE_1"])
        self.assertEqual("core.safecrlf", environment["GIT_CONFIG_KEY_2"])
        self.assertEqual("true", environment["GIT_CONFIG_VALUE_2"])

    def test_rejects_unbounded_or_incomplete_inherited_git_config(self):
        with self.assertRaisesRegex(RuntimeError, "bounded decimal"):
            configure_test_git_environment({"GIT_CONFIG_COUNT": "invalid"})
        with self.assertRaisesRegex(RuntimeError, "exceeds 64"):
            configure_test_git_environment({"GIT_CONFIG_COUNT": "65"})
        with self.assertRaisesRegex(RuntimeError, "GIT_CONFIG_KEY_0"):
            configure_test_git_environment({"GIT_CONFIG_COUNT": "1"})

    def test_conflicting_inherited_autocrlf_emits_no_fixture_warning(self):
        environment = {
            **os.environ,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.autocrlf",
            "GIT_CONFIG_VALUE_0": "true",
        }
        configure_test_git_environment(environment)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(
                ["git", "init", "-q"], cwd=root, env=environment, check=True
            )
            (root / "fixture.txt").write_bytes(b"line\n")
            result = subprocess.run(
                ["git", "add", "fixture.txt"],
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(0, result.returncode)
        self.assertEqual(b"", result.stderr)


if __name__ == "__main__":
    unittest.main()
