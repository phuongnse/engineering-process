from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from engineering_process.repository import repository_snapshot, same_checkpoint


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


if __name__ == "__main__":
    unittest.main()
