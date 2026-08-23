import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from engineering_process.contracts import ContractError
from verification.qualify_release_lifecycle import (
    _safe_environment,
    pending_release_changes,
    qualification_evidence,
    qualify_release_lifecycle,
)


class ReleaseQualificationTests(unittest.TestCase):
    def test_pending_changes_are_sorted_and_bounded_to_json_fragments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            changes = root / "release-changes"
            changes.mkdir()
            (changes / "README.md").write_text("contract\n", encoding="utf-8")
            (changes / "zeta.json").write_text("{}\n", encoding="utf-8")
            (changes / "alpha.json").write_text("{}\n", encoding="utf-8")

            result = pending_release_changes(root)

            self.assertEqual(["alpha.json", "zeta.json"], [path.name for path in result])

    def test_qualification_evidence_is_explicitly_bound_to_exact_checkpoint(self):
        evidence = qualification_evidence("a" * 40)

        self.assertEqual("a" * 40, evidence["headSha"])
        self.assertEqual("policy-verification", evidence["verificationKind"])
        self.assertEqual("phuongnse/renovate-ops", evidence["verifierRepository"])
        self.assertEqual(40, len(evidence["verifierSha"]))

    def test_qualification_subprocesses_do_not_receive_secret_environment(self):
        environment = {
            "PATH": "/usr/bin",
            "GITHUB_TOKEN": "secret",
            "PYPI_PASSWORD": "secret",
            "APP_PRIVATE_KEY": "secret",
            "VISIBLE_SETTING": "safe",
        }
        with patch.dict(os.environ, environment, clear=True):
            result = _safe_environment()

        self.assertEqual(
            str(Path(sys.executable).absolute().parent),
            result["PATH"].split(os.pathsep)[0],
        )
        self.assertTrue(result["PATH"].endswith("/usr/bin"))
        self.assertEqual("safe", result["VISIBLE_SETTING"])
        self.assertNotIn("GITHUB_TOKEN", result)
        self.assertNotIn("PYPI_PASSWORD", result)
        self.assertNotIn("APP_PRIVATE_KEY", result)

    def test_no_pending_release_is_an_explicit_non_applicable_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "release-changes").mkdir()

            result = qualify_release_lifecycle(root, Path(sys.executable))

        self.assertEqual(
            {"status": "not-applicable", "reason": "no pending release changes"},
            result,
        )

    def test_qualification_refuses_a_dirty_source_checkpoint_before_cloning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Qualification Test"],
                cwd=root,
                check=True,
            )
            changes = root / "release-changes"
            changes.mkdir()
            (changes / "pending.json").write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
            (root / "unexpected.txt").write_text("dirty\n", encoding="utf-8")

            with self.assertRaisesRegex(ContractError, "clean source checkpoint"):
                qualify_release_lifecycle(root, Path(sys.executable))


if __name__ == "__main__":
    unittest.main()
