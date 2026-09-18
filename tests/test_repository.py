from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from engineering_process.contracts import ProcessError
from engineering_process import repository
from engineering_process.repository import (
    changed_paths, remove_owned_runtime, repository_snapshot,
    require_committed_candidate, resolve_commit, runtime_storage_usage,
    same_checkpoint,
)


def git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


class RepositorySnapshotTests(unittest.TestCase):
    def make_repository(self, root: Path) -> str:
        git(root, "init", "-q")
        git(root, "config", "user.email", "tests@example.invalid")
        git(root, "config", "user.name", "Tests")
        (root / ".gitignore").write_text("/.process/runs/\n/.process/receipts/\n", encoding="utf-8")
        (root / "tracked.txt").write_text("one\n", encoding="utf-8")
        git(root, "add", ".")
        git(root, "commit", "-qm", "initial")
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()

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

    def test_changed_paths_includes_worktree_add_delete_and_untracked_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.make_repository(root)
            (root / "tracked.txt").unlink()
            (root / "added.txt").write_text("added\n", encoding="utf-8")
            (root / "untracked.txt").write_text("untracked\n", encoding="utf-8")
            self.assertEqual(
                ("added.txt", "tracked.txt", "untracked.txt"),
                changed_paths(root, base),
            )

    @unittest.skipUnless(os.name == "posix", "POSIX filename semantics")
    def test_changed_paths_preserves_literal_backslash_on_posix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.make_repository(root)
            literal = root / r"src\secret.py"
            literal.write_text("secret\n", encoding="utf-8")
            self.assertEqual((r"src\secret.py",), changed_paths(root, base))

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

    @unittest.skipUnless(os.name == "posix", "POSIX link semantics")
    def test_owned_runtime_cleanup_is_scoped_and_does_not_follow_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repository(root)
            outside = root / "outside.txt"
            outside.write_text("preserve\n", encoding="utf-8")
            first = root / ".process" / "runs" / "first"
            second = root / ".process" / "runs" / "second"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (first / "run.json").write_text("first\n", encoding="utf-8")
            (second / "run.json").write_text("second\n", encoding="utf-8")
            (first / "outside-link").symlink_to(outside)

            result = remove_owned_runtime(root, "first")

            self.assertEqual(2, result["removedArtifacts"])
            self.assertFalse(first.exists())
            self.assertTrue((second / "run.json").is_file())
            self.assertEqual("preserve\n", outside.read_text(encoding="utf-8"))
            usage = runtime_storage_usage(root)
            self.assertEqual(1, usage["runs"]["fileCount"])
            self.assertEqual(2, usage["runs"]["directoryCount"])

    def test_owned_runtime_cleanup_removes_empty_root_after_target_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repository(root)
            runs = root / ".process" / "runs"
            target = runs / "first"
            target.mkdir(parents=True)
            target.rmdir()

            result = remove_owned_runtime(root, "first")

            self.assertEqual(
                {"removedArtifacts": 0, "remainingArtifacts": 0}, result
            )
            self.assertFalse(runs.exists())
            self.assertEqual(0, runtime_storage_usage(root)["runs"]["directoryCount"])

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

    def test_committed_candidate_inspects_hidden_files_without_changing_index_flags(self) -> None:
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.make_repository(root)
                git(root, "update-index", flag, "tracked.txt")
                index = root / ".git/index"
                before = index.read_bytes()
                require_committed_candidate(root)
                self.assertEqual(before, index.read_bytes())
                (root / "tracked.txt").write_text("two\n", encoding="utf-8")
                with self.assertRaisesRegex(ProcessError, "committed candidate changes"):
                    require_committed_candidate(root)
                self.assertEqual(before, index.read_bytes())

    def test_committed_candidate_does_not_follow_a_transient_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repository(root)
            original = resolve_commit(root, "HEAD")
            (root / "tracked.txt").write_text("two\n", encoding="utf-8")
            git(root, "add", "tracked.txt")
            git(root, "commit", "-qm", "fix: alternate content")
            transient = resolve_commit(root, "HEAD")
            git(root, "reset", "--mixed", original)
            before = repository_snapshot(root)
            inspect = repository._git

            def change_head(path: Path, arguments: list[str], **options) -> bytes:
                if "read-tree" in arguments:
                    git(root, "update-ref", "HEAD", transient)
                try:
                    return inspect(path, arguments, **options)
                finally:
                    if "status" in arguments:
                        git(root, "update-ref", "HEAD", original)

            try:
                with patch("engineering_process.repository._git", side_effect=change_head):
                    with self.assertRaisesRegex(ProcessError, "committed candidate changes"):
                        require_committed_candidate(root)
            finally:
                git(root, "update-ref", "HEAD", original)
            self.assertTrue(same_checkpoint(before, repository_snapshot(root)))

    def test_committed_candidate_preserves_sparse_omissions_but_inspects_present_files(self) -> None:
        for mode in ("--no-sparse-index", "--sparse-index"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.make_repository(root)
                for relative in ("selected/kept.txt", "omitted/absent.txt"):
                    path = root / relative
                    path.parent.mkdir(parents=True)
                    path.write_text("committed\n", encoding="utf-8")
                git(root, "add", ".")
                git(root, "commit", "-qm", "fix: sparse fixture")
                git(root, "sparse-checkout", "init", "--cone", mode)
                git(root, "sparse-checkout", "set", "selected")
                absent = root / "omitted/absent.txt"
                self.assertFalse(absent.exists())
                index = root / ".git/index"
                before = index.read_bytes()
                require_committed_candidate(root)
                self.assertEqual(before, index.read_bytes())
                absent.parent.mkdir(exist_ok=True)
                absent.write_text("uncommitted\n", encoding="utf-8")
                with self.assertRaisesRegex(ProcessError, "committed candidate changes"):
                    require_committed_candidate(root)
                self.assertEqual(before, index.read_bytes())

    def test_repository_local_temporary_root_does_not_join_the_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repository(root)
            before = repository_snapshot(root)
            index = (root / ".git/index").read_bytes()
            with patch("engineering_process.repository.tempfile.tempdir", str(root)):
                require_committed_candidate(root)
            self.assertTrue(same_checkpoint(before, repository_snapshot(root)))
            self.assertEqual(index, (root / ".git/index").read_bytes())
            self.assertEqual([], list((root / ".git").glob("process-candidate-*")))

    def test_committed_candidate_supports_linked_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            root.mkdir()
            self.make_repository(root)
            linked = Path(directory) / "linked"
            git(root, "worktree", "add", "--detach", str(linked), "HEAD")
            before = repository_snapshot(linked)
            with patch("engineering_process.repository.tempfile.tempdir", str(linked)):
                require_committed_candidate(linked)
            self.assertTrue(same_checkpoint(before, repository_snapshot(linked)))
            (linked / "tracked.txt").write_text("uncommitted\n", encoding="utf-8")
            with self.assertRaisesRegex(ProcessError, "committed candidate changes"):
                require_committed_candidate(linked)

    def test_file_digest_cache_reuses_clean_reads_and_invalidates_on_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repository(root)
            first = repository_snapshot(root)
            second = repository_snapshot(root)
            self.assertTrue(same_checkpoint(first, second))
            self.assertEqual(first["fingerprint"], second["fingerprint"])

            # Mutate tracked file
            tracked = root / "tracked.txt"
            time.sleep(0.01)
            tracked.write_text("modified content\n", encoding="utf-8")
            third = repository_snapshot(root)
            self.assertFalse(same_checkpoint(first, third))
            self.assertNotEqual(first["fingerprint"], third["fingerprint"])

    def test_file_digest_cache_rejects_same_size_same_mtime_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repository(root)
            tracked = root / "tracked.txt"
            original_stat = tracked.stat()
            first = repository_snapshot(root)

            tracked.write_text("two\n", encoding="utf-8")
            os.utime(
                tracked,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            second = repository_snapshot(root)

        self.assertFalse(same_checkpoint(first, second))


if __name__ == "__main__":
    unittest.main()
