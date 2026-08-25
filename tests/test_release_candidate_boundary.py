import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from engineering_process.contracts import ContractError
from verification.validate_release_candidate_commit import (
    validate_release_candidate_commit,
)


PROCESS_ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = PROCESS_ROOT / "verification" / "validate_release_candidate_commit.py"


class ReleaseCandidateBoundaryTests(unittest.TestCase):
    def _run(self, root: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _candidate(self, directory: str) -> tuple[Path, str]:
        root = Path(directory)
        self._run(root, "init", "-q", "-b", "main")
        self._run(root, "config", "user.email", "release-test@example.invalid")
        self._run(root, "config", "user.name", "Release Test")
        tracked = root / "tracked.txt"
        tracked.write_text("base\n", encoding="utf-8")
        self._run(root, "add", "--all")
        self._run(root, "commit", "-qm", "chore: establish release base")
        base_sha = self._run(root, "rev-parse", "HEAD")
        tracked.write_text("candidate\n", encoding="utf-8")
        self._run(root, "add", "--all")
        self._run(root, "commit", "-qm", "chore(release): prepare v0.2.0")
        return root, base_sha

    def test_accepts_one_clean_exact_candidate_commit_through_cli_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root, base_sha = self._candidate(directory)

            result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR),
                    "--project-root",
                    str(root),
                    "--expected-base",
                    base_sha,
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(base_sha, summary["baseSha"])
            self.assertEqual(self._run(root, "rev-parse", "HEAD"), summary["headSha"])
            self.assertEqual("clean", summary["status"])

    def test_rejects_every_residual_candidate_output(self):
        cases = ("tracked", "staged", "untracked")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root, base_sha = self._candidate(directory)
                if case == "untracked":
                    (root / "generated.json").write_text("{}\n", encoding="utf-8")
                else:
                    (root / "tracked.txt").write_text("residual\n", encoding="utf-8")
                    if case == "staged":
                        self._run(root, "add", "tracked.txt")

                with self.assertRaisesRegex(ContractError, "uncommitted"):
                    validate_release_candidate_commit(root, expected_base=base_sha)

    def test_rejects_a_candidate_with_the_wrong_protected_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _base_sha = self._candidate(directory)

            with self.assertRaisesRegex(ContractError, "protected source checkpoint"):
                validate_release_candidate_commit(root, expected_base="a" * 40)


if __name__ == "__main__":
    unittest.main()
