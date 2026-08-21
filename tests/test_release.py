import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engineering_process.contracts import ContractError
from engineering_process.release import validate_release_checkpoint


class ReleaseCheckpointTests(unittest.TestCase):
    def initialize_repository(self, root: Path) -> None:
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

    def write_contract(self, root: Path, *, previous: str = "0.1.1") -> str:
        (root / ".process").mkdir()
        (root / ".process" / "process.lock").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "process": {
                        "version": "0.1.1",
                        "digest": f"sha256:{'0' * 64}",
                    },
                    "skills": ["run-change"],
                }
            ),
            encoding="utf-8",
        )
        (root / "pyproject.toml").write_text(
            '[project]\nname = "sample"\nversion = "0.2.0"\n',
            encoding="utf-8",
        )
        (root / "sample_runtime.py").write_text('VERSION = "0.2.0"\n', encoding="utf-8")
        (root / "release.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "previousVersion": previous,
                    "version": "0.2.0",
                    "classification": "minor",
                    "compatibility": "backward-compatible",
                    "schemaImpact": "additive",
                    "migration": None,
                    "identity": {
                        "package": "sample",
                        "distribution": "sample",
                        "tag": "v0.2.0",
                        "releaseName": "v0.2.0",
                        "runtimeVersion": {
                            "path": "sample_runtime.py",
                            "variable": "VERSION",
                        },
                        "artifacts": [
                            "sample-0.2.0-py3-none-any.whl",
                            "sample-0.2.0.tar.gz",
                        ],
                        "receiptAsset": "sample-v0.2.0-evidence.json",
                    },
                    "provenance": {
                        "mode": "governed",
                        "statement": "The public N-1 receipt binds this release.",
                        "lifecycleReceipt": {
                            "asset": "sample-v0.2.0-evidence.json",
                            "project": "sample",
                            "changeId": "release-0-2-0",
                            "cycle": 2,
                        },
                    },
                    "changes": [
                        {
                            "id": "portable-capability",
                            "type": "capability",
                            "surfaces": ["cli"],
                            "rationale": "Add the portable capability.",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (root / "tracked.txt").write_text("two\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "feat: add capability"], cwd=root, check=True
        )
        subprocess.run(["git", "tag", "v0.2.0"], cwd=root, check=True)
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()

    def receipt_result(self, checkpoint: str) -> dict[str, object]:
        return {
            "changeId": "release-0-2-0",
            "project": "sample",
            "cycle": 2,
            "checkpoint": checkpoint,
            "processVersion": "0.1.1",
            "processDigest": f"sha256:{'0' * 64}",
            "stateCanonicalDigest": f"sha256:{'1' * 64}",
            "sha256": f"sha256:{'2' * 64}",
        }

    def validate(self, root: Path, receipt: Path, checkpoint: str, **overrides):
        arguments = {
            "tag": "v0.2.0",
            "release_name": "v0.2.0",
            "commit": checkpoint,
            "main_ref": "main",
            "receipt_path": receipt,
        }
        arguments.update(overrides)
        with patch(
            "engineering_process.release.validate_receipt",
            return_value=self.receipt_result(checkpoint),
        ):
            return validate_release_checkpoint(root, **arguments)

    def test_binds_all_release_identity_surfaces_and_previous_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            root.mkdir()
            self.initialize_repository(root)
            checkpoint = self.write_contract(root)
            receipt = base / "sample-v0.2.0-evidence.json"
            receipt.write_text("{}", encoding="utf-8")

            result = self.validate(root, receipt, checkpoint)

            self.assertEqual("v0.1.1", result["previousTag"])
            self.assertEqual(checkpoint, result["checkpoint"])
            self.assertEqual("v0.2.0", result["releaseName"])

    def test_rejects_release_title_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            root.mkdir()
            self.initialize_repository(root)
            checkpoint = self.write_contract(root)
            receipt = base / "sample-v0.2.0-evidence.json"
            receipt.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ContractError, "release name"):
                self.validate(
                    root,
                    receipt,
                    checkpoint,
                    release_name="sample 0.2.0",
                )

    def test_rejects_skipping_the_latest_reachable_release(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            root.mkdir()
            self.initialize_repository(root)
            checkpoint = self.write_contract(root)
            subprocess.run(["git", "tag", "v0.1.2", "HEAD~1"], cwd=root, check=True)
            receipt = base / "sample-v0.2.0-evidence.json"
            receipt.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ContractError, "latest reachable release 0.1.2"):
                self.validate(root, receipt, checkpoint)

    def test_accepts_reviewed_ancestor_with_the_identical_release_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            root.mkdir()
            self.initialize_repository(root)
            reviewed = self.write_contract(root)
            subprocess.run(
                ["git", "commit", "--allow-empty", "-qm", "chore: merge release"],
                cwd=root,
                check=True,
            )
            checkpoint = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            subprocess.run(
                ["git", "tag", "-f", "v0.2.0", checkpoint], cwd=root, check=True
            )
            receipt = base / "sample-v0.2.0-evidence.json"
            receipt.write_text("{}", encoding="utf-8")

            with patch(
                "engineering_process.release.validate_receipt",
                return_value=self.receipt_result(reviewed),
            ):
                result = validate_release_checkpoint(
                    root,
                    tag="v0.2.0",
                    release_name="v0.2.0",
                    commit=checkpoint,
                    main_ref="main",
                    receipt_path=receipt,
                    reviewed_commit=reviewed,
                )

            self.assertEqual(reviewed, result["reviewedCheckpoint"])
            self.assertEqual(checkpoint, result["checkpoint"])

    def test_accepts_squash_commit_with_the_identical_reviewed_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            root.mkdir()
            self.initialize_repository(root)
            reviewed = self.write_contract(root)
            parent = subprocess.check_output(
                ["git", "rev-parse", f"{reviewed}^"], cwd=root, text=True
            ).strip()
            tree = subprocess.check_output(
                ["git", "rev-parse", f"{reviewed}^{{tree}}"], cwd=root, text=True
            ).strip()
            checkpoint = subprocess.check_output(
                ["git", "commit-tree", tree, "-p", parent],
                cwd=root,
                text=True,
                input="chore: squash reviewed release\n",
            ).strip()
            subprocess.run(["git", "reset", "--hard", checkpoint], cwd=root, check=True)
            subprocess.run(
                ["git", "tag", "-f", "v0.2.0", checkpoint], cwd=root, check=True
            )
            receipt = base / "sample-v0.2.0-evidence.json"
            receipt.write_text("{}", encoding="utf-8")

            with patch(
                "engineering_process.release.validate_receipt",
                return_value=self.receipt_result(reviewed),
            ):
                result = validate_release_checkpoint(
                    root,
                    tag="v0.2.0",
                    release_name="v0.2.0",
                    commit=checkpoint,
                    main_ref="main",
                    receipt_path=receipt,
                    reviewed_commit=reviewed,
                )

            self.assertEqual(reviewed, result["reviewedCheckpoint"])
            self.assertEqual(checkpoint, result["checkpoint"])

    def test_accepts_one_bootstrap_authority_after_uncontracted_history(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            root.mkdir()
            self.initialize_repository(root)
            (root / ".process").mkdir()
            (root / ".process" / "process.lock").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "process": {
                            "version": "0.1.1",
                            "digest": f"sha256:{'0' * 64}",
                        },
                        "skills": ["run-change"],
                    }
                ),
                encoding="utf-8",
            )
            (root / "pyproject.toml").write_text(
                '[project]\nname = "sample"\nversion = "0.2.0"\n',
                encoding="utf-8",
            )
            (root / "sample_runtime.py").write_text(
                'VERSION = "0.2.0"\n', encoding="utf-8"
            )
            authorization_name = "sample-v0.2.0-bootstrap-authorization.json"
            (root / "release.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 3,
                        "previousVersion": "0.1.1",
                        "version": "0.2.0",
                        "classification": "minor",
                        "compatibility": "backward-compatible",
                        "schemaImpact": "additive",
                        "migration": None,
                        "identity": {
                            "package": "sample",
                            "distribution": "sample",
                            "tag": "v0.2.0",
                            "releaseName": "v0.2.0",
                            "runtimeVersion": {
                                "path": "sample_runtime.py",
                                "variable": "VERSION",
                            },
                            "artifacts": [
                                "sample-0.2.0-py3-none-any.whl",
                                "sample-0.2.0.tar.gz",
                            ],
                            "receiptAsset": None,
                            "authorizationAsset": authorization_name,
                        },
                        "provenance": {
                            "mode": "bootstrap-authority",
                            "statement": "Publish the first public evidence authority.",
                            "lifecycleReceipt": None,
                        },
                        "changes": [
                            {
                                "id": "public-evidence-authority",
                                "type": "capability",
                                "surfaces": ["evidence"],
                                "rationale": "Publish the portable evidence validator.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "feat: prepare bootstrap authority"],
                cwd=root,
                check=True,
            )
            reviewed = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            subprocess.run(
                ["git", "commit", "--allow-empty", "-qm", "chore: merge release"],
                cwd=root,
                check=True,
            )
            checkpoint = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            subprocess.run(["git", "tag", "v0.2.0"], cwd=root, check=True)
            authorization = base / authorization_name
            authorization.write_text("{}", encoding="utf-8")
            authorization_result = {
                "project": "sample",
                "changeId": "release-0-2-0",
                "cycle": 1,
                "checkpoint": reviewed,
                "processVersion": "0.1.1",
                "processDigest": f"sha256:{'0' * 64}",
            }

            with patch(
                "engineering_process.release.validate_bootstrap_authorization",
                return_value=authorization_result,
            ):
                result = validate_release_checkpoint(
                    root,
                    tag="v0.2.0",
                    release_name="v0.2.0",
                    commit=checkpoint,
                    main_ref="main",
                    authorization_path=authorization,
                    reviewed_commit=reviewed,
                )

            self.assertEqual("bootstrap-authority", result["provenanceMode"])
            self.assertEqual(reviewed, result["reviewedCheckpoint"])

    def test_rejects_merge_commit_whose_tree_differs_from_reviewed_head(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            root.mkdir()
            self.initialize_repository(root)
            reviewed = self.write_contract(root)
            (root / "tracked.txt").write_text("changed after review\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "fix: change reviewed tree"],
                cwd=root,
                check=True,
            )
            checkpoint = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            subprocess.run(
                ["git", "tag", "-f", "v0.2.0", checkpoint], cwd=root, check=True
            )
            receipt = base / "sample-v0.2.0-evidence.json"
            receipt.write_text("{}", encoding="utf-8")

            with patch(
                "engineering_process.release.validate_receipt",
                return_value=self.receipt_result(reviewed),
            ):
                with self.assertRaisesRegex(ContractError, "reviewed tree"):
                    validate_release_checkpoint(
                        root,
                        tag="v0.2.0",
                        release_name="v0.2.0",
                        commit=checkpoint,
                        main_ref="main",
                        receipt_path=receipt,
                        reviewed_commit=reviewed,
                    )


if __name__ == "__main__":
    unittest.main()
