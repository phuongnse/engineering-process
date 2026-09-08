from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from engineering_process.contracts import ProcessError
from tests.test_publication_compat import CANONICAL_BODY
from verification.verify_publication import verify_publication


class PublicationWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Tests")
        self.git("config", "user.email", "tests@example.invalid")
        (self.root / "product.txt").write_text("before\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "chore: initial")
        base = self.git("rev-parse", "HEAD")
        self.git("switch", "-c", "fix/actual-proposal")
        (self.root / "product.txt").write_text("after\n", encoding="utf-8")
        self.git("commit", "-qam", "fix: preserve behavior")
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("PUBLICATION_")}
        self.environment.update({
            "PUBLICATION_BRANCH": "fix/actual-proposal",
            "PUBLICATION_TITLE": "fix: preserve behavior",
            "PUBLICATION_BODY": CANONICAL_BODY,
            "PUBLICATION_DRAFT": "false",
            "PUBLICATION_BASE": base,
            "PUBLICATION_HEAD": self.git("rev-parse", "HEAD"),
        })

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", *arguments], cwd=self.root, capture_output=True, text=True, timeout=15, check=True).stdout.strip()

    def test_local_verification_uses_the_actual_branch(self) -> None:
        self.assertEqual([], verify_publication(self.root, pull_request=False))
        self.git("branch", "-m", "codex/incorrect-prefix")
        self.assertTrue(verify_publication(self.root, pull_request=False))
        self.git("switch", "main")
        self.assertEqual([], verify_publication(self.root, pull_request=False))
        self.git("checkout", "--detach")
        with self.assertRaisesRegex(ProcessError, "named branch"):
            verify_publication(self.root, pull_request=False)

    def test_pr_context_validates_detached_checkout_and_real_metadata(self) -> None:
        self.git("checkout", "--detach")
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual([], verify_publication(self.root, pull_request=True))
            for field, value in (("BRANCH", "codex/incorrect"), ("TITLE", "unstructured title"), ("BODY", "missing public evidence")):
                with self.subTest(field=field), patch.dict(os.environ, {f"PUBLICATION_{field}": value}):
                    self.assertTrue(verify_publication(self.root, pull_request=True))

    def test_draft_to_ready_rechecks_completion_evidence(self) -> None:
        self.environment["PUBLICATION_BODY"] = CANONICAL_BODY.replace("[x]", "[ ]")
        self.environment["PUBLICATION_DRAFT"] = "true"
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual([], verify_publication(self.root, pull_request=True))
            os.environ["PUBLICATION_DRAFT"] = "false"
            self.assertTrue(verify_publication(self.root, pull_request=True))

    def test_missing_or_invalid_context_fails(self) -> None:
        for field, value in (("BODY", None), ("DRAFT", "unknown"), ("HEAD", "HEAD"), ("BASE", "--all")):
            environment = self.environment.copy()
            if value is None:
                del environment[f"PUBLICATION_{field}"]
            else:
                environment[f"PUBLICATION_{field}"] = value
            with self.subTest(field=field), patch.dict(os.environ, environment, clear=True), self.assertRaises(ProcessError):
                verify_publication(self.root, pull_request=True)

    def test_metadata_is_data_and_real_commit_subjects_are_checked(self) -> None:
        marker = self.root / "injected.txt"
        self.environment["PUBLICATION_TITLE"] = f"fix: $(echo injected > {marker})"
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual([], verify_publication(self.root, pull_request=True))
        self.assertFalse(marker.exists())
        self.git("commit", "--amend", "-qm", "unstructured subject")
        self.environment["PUBLICATION_HEAD"] = self.git("rev-parse", "HEAD")
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertTrue(any("Conventional Commit" in issue for issue in verify_publication(self.root, pull_request=True)))


if __name__ == "__main__":
    unittest.main()
