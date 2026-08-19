import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from engineering_process.contracts import ContractError
from engineering_process.release import validate_release_checkpoint


class ReleaseCheckpointTests(unittest.TestCase):
    def initialize_repository(self, root: Path) -> str:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "process-test@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Process Test"], cwd=root, check=True
        )
        (root / "tracked.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "chore: initialize release fixture"],
            cwd=root,
            check=True,
        )
        subprocess.run(["git", "tag", "v0.1.1"], cwd=root, check=True)
        (root / "tracked.txt").write_text("two\n", encoding="utf-8")
        subprocess.run(
            ["git", "commit", "-qam", "feat: add capability"], cwd=root, check=True
        )
        subprocess.run(["git", "tag", "v0.2.0"], cwd=root, check=True)
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()

    def write_contract(self, root: Path, *, previous: str = "0.1.1") -> None:
        (root / "pyproject.toml").write_text(
            '[project]\nname = "sample"\nversion = "0.2.0"\n',
            encoding="utf-8",
        )
        (root / "release.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "previousVersion": previous,
                    "version": "0.2.0",
                    "classification": "minor",
                    "compatibility": "backward-compatible",
                    "schemaImpact": "additive",
                    "migration": None,
                }
            ),
            encoding="utf-8",
        )

    def test_binds_contract_version_previous_tag_checkpoint_and_main(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = self.initialize_repository(root)
            self.write_contract(root)

            result = validate_release_checkpoint(
                root,
                tag="v0.2.0",
                commit=checkpoint,
                main_ref="main",
            )

            self.assertEqual("v0.1.1", result["previousTag"])
            self.assertEqual(checkpoint, result["checkpoint"])

    def test_rejects_skipping_the_latest_reachable_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = self.initialize_repository(root)
            subprocess.run(["git", "tag", "v0.1.2", "HEAD~1"], cwd=root, check=True)
            self.write_contract(root)

            with self.assertRaisesRegex(ContractError, "latest reachable release 0.1.2"):
                validate_release_checkpoint(
                    root,
                    tag="v0.2.0",
                    commit=checkpoint,
                    main_ref="main",
                )


if __name__ == "__main__":
    unittest.main()
