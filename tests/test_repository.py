from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from engineering_process.contracts import ProcessError
from engineering_process.repository import (
    repository_snapshot, require_committed_candidate, resolve_commit, same_checkpoint,
)


def git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


class RepositorySnapshotTests(unittest.TestCase):
    def make_repository(self, root: Path) -> None:
        git(root, "init", "-q")
        git(root, "config", "user.email", "tests@example.invalid")
        git(root, "config", "user.name", "Tests")
        (root / ".gitignore").write_text("/.process/runs/\n/.process/receipts/\n", encoding="utf-8")
        (root / "tracked.txt").write_text("one\n", encoding="utf-8")
        git(root, "add", ".")
        git(root, "commit", "-qm", "initial")

    def test_tracked_and_untracked_content_change_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repository(root)
            initial = repository_snapshot(root)
            (root / "tracked.txt").write_text("two\n", encoding="utf-8")
            tracked = repository_snapshot(root)
            self.assertFalse(same_checkpoint(initial, tracked))
            (root / "new.txt").write_text("new\n", encoding="utf-8")
            untracked = repository_snapshot(root)
            self.assertFalse(same_checkpoint(tracked, untracked))

    def test_resolve_commit_accepts_branches_and_peels_annotated_tags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repository(root)
            git(root, "branch", "review-base")
            git(root, "tag", "-a", "review-tag", "-m", "Review boundary")
            expected = repository_snapshot(root)["head"]
            for reference in ("HEAD", "review-base", "review-tag"):
                with self.subTest(reference=reference):
                    self.assertEqual(expected, resolve_commit(root, reference))

    def test_lifecycle_state_does_not_invalidate_its_own_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repository(root)
            initial = repository_snapshot(root)
            state = root / ".process" / "runs" / "change" / "run.json"
            state.parent.mkdir(parents=True)
            state.write_text("{}\n", encoding="utf-8")
            receipt = root / ".process" / "receipts" / "change.json"
            receipt.parent.mkdir(parents=True)
            receipt.write_text("{}\n", encoding="utf-8")
            self.assertTrue(same_checkpoint(initial, repository_snapshot(root)))

    def test_committed_candidate_ignores_only_lifecycle_state_and_ignored_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repository(root)
            # State remains excluded even in consumers without matching ignore rules.
            (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
            git(root, "add", ".gitignore")
            git(root, "commit", "-qm", "fix: consumer ignore rules")
            for relative in (".process/runs/sample/run.json", ".process/receipts/sample.json", "ignored.txt"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("local\n", encoding="utf-8")
            require_committed_candidate(root)
            (root / ".process/project.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ProcessError, "committed candidate changes"):
                require_committed_candidate(root)

    def test_committed_candidate_rejects_worktree_index_and_untracked_changes(self) -> None:
        for change in ("unstaged", "staged", "index-only", "untracked", "deleted", "renamed"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.make_repository(root)
                tracked = root / "tracked.txt"
                if change == "untracked":
                    (root / "new name-é.txt").write_text("new\n", encoding="utf-8")
                elif change == "deleted":
                    tracked.unlink()
                elif change == "renamed":
                    git(root, "mv", "tracked.txt", "renamed.txt")
                else:
                    tracked.write_text("two\n", encoding="utf-8")
                    if change in ("staged", "index-only"):
                        git(root, "add", "tracked.txt")
                    if change == "index-only":
                        tracked.write_text("one\n", encoding="utf-8")
                with self.assertRaisesRegex(ProcessError, "committed candidate changes.*before change verify"):
                    require_committed_candidate(root)


if __name__ == "__main__":
    unittest.main()
