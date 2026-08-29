from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from engineering_process.publication_compat import (
    branch_issues,
    commit_issues,
    validate_pull_request,
    validate_range,
)


class PublicationCompatibilityTests(unittest.TestCase):
    def test_branch_and_commit_conventions(self) -> None:
        self.assertEqual([], branch_issues("feature/small-change"))
        self.assertEqual([], branch_issues("automation/renovate/engineering-process"))
        self.assertTrue(branch_issues("main"))
        self.assertEqual([], commit_issues("fix(core): preserve behavior"))
        self.assertTrue(commit_issues("unstructured subject"))

    def test_pull_request_requires_core_review_sections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            body = Path(directory) / "body.md"
            body.write_text(
                "## Summary\nchange\n## Verification\npass\n## Independent review\napproved\n",
                encoding="utf-8",
            )
            result = validate_pull_request(
                title="fix(core): preserve behavior",
                branch="fix/preserve-behavior",
                state="ready",
                body_path=body,
            )
        self.assertEqual([], result["issues"])

    def test_range_checks_every_commit_subject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "tests@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Tests"], cwd=root, check=True
            )
            (root / "file.txt").write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "chore: initial"], cwd=root, check=True
            )
            base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            (root / "file.txt").write_text("two\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-qam", "fix(core): update"], cwd=root, check=True)
            result = validate_range(root, "fix/update", f"{base}..HEAD")
        self.assertEqual([], result["issues"])
        self.assertEqual(1, len(result["commits"]))


if __name__ == "__main__":
    unittest.main()
