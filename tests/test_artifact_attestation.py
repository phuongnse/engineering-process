import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engineering_process.artifact_attestation import (
    _artifact_entries,
    _bootstrap_authorization_identity,
    create_distribution_attestation,
    validate_distribution_attestation,
)
from engineering_process.contracts import ContractError, Release


class ArtifactAttestationTests(unittest.TestCase):
    def test_bootstrap_authorization_binds_reviewed_tree_to_release_commit(self):
        release = Release(
            previous_version="0.1.1",
            version="0.2.0",
            classification="minor",
            compatibility="backward-compatible",
            schema_impact="additive",
            migration=None,
            package_name="sample",
            authorization_asset="sample-v0.2.0-bootstrap-authorization.json",
            provenance_mode="bootstrap-authority",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authorization = root / release.authorization_asset
            authorization.write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "tests@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Tests"], cwd=root, check=True
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "reviewed release tree"],
                cwd=root,
                check=True,
            )
            reviewed = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            subprocess.run(
                ["git", "commit", "--allow-empty", "-qm", "merge release"],
                cwd=root,
                check=True,
            )
            checkpoint = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            summary = {
                "project": "sample",
                "changeId": "release-0-2-0",
                "cycle": 1,
                "checkpoint": reviewed,
                "processVersion": "0.1.1",
                "processDigest": f"sha256:{'0' * 64}",
            }

            with patch(
                "engineering_process.artifact_attestation.validate_bootstrap_authorization",
                return_value=summary,
            ):
                identity = _bootstrap_authorization_identity(
                    root,
                    release,
                    authorization,
                    checkpoint=checkpoint,
                )

            self.assertEqual(reviewed, identity["checkpoint"])
            self.assertEqual(authorization.name, identity["asset"])

    def test_artifact_enumeration_is_count_name_and_time_bounded(self):
        release = Release(
            previous_version="0.1.0",
            version="0.1.1",
            classification="patch",
            compatibility="backward-compatible",
            schema_impact="unchanged",
            migration=None,
            artifacts=("one.whl", "two.tar.gz"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.whl").write_bytes(b"one")
            (root / "two.tar.gz").write_bytes(b"two")
            self.assertEqual(2, len(_artifact_entries(root, release)))

            (root / "extra.bin").write_bytes(b"extra")
            with self.assertRaisesRegex(ContractError, "declared artifact count"):
                _artifact_entries(root, release)
            (root / "extra.bin").unlink()
            with (
                patch(
                    "engineering_process.artifact_attestation.ARTIFACT_ENUMERATION_TIMEOUT_SECONDS",
                    0,
                ),
                self.assertRaisesRegex(ContractError, "enumeration exceeded 0 seconds"),
            ):
                _artifact_entries(root, release)
            with (
                patch(
                    "engineering_process.artifact_attestation.MAX_ARTIFACT_NAME_BYTES",
                    1,
                ),
                self.assertRaisesRegex(ContractError, "names exceed 1 bytes"),
            ):
                _artifact_entries(root, release)

    def test_attestation_binds_artifact_bytes_release_checkpoint_and_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            artifacts = base / "artifacts"
            root.mkdir()
            artifacts.mkdir()
            release = {
                "schemaVersion": 2,
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
                        "id": "artifact-attestation",
                        "type": "capability",
                        "surfaces": ["release"],
                        "rationale": "Bind published bytes to durable release evidence.",
                    }
                ],
            }
            (root / "release.json").write_text(
                json.dumps(release), encoding="utf-8"
            )
            receipt = root / "sample-v0.2.0-evidence.json"
            receipt.write_text('{"bounded":"receipt"}\n', encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "tests@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Tests"], cwd=root, check=True
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "release checkpoint"],
                cwd=root,
                check=True,
            )
            checkpoint = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            for name, content in (
                ("sample-0.2.0-py3-none-any.whl", b"wheel"),
                ("sample-0.2.0.tar.gz", b"sdist"),
            ):
                (artifacts / name).write_bytes(content)
            receipt_summary = {
                "project": "sample",
                "changeId": "release-0-2-0",
                "cycle": 2,
                "checkpoint": checkpoint,
                "processVersion": "0.1.1",
                "processDigest": f"sha256:{'0' * 64}",
            }
            attestation = base / "sample-v0.2.0-artifacts.json"

            with patch(
                "engineering_process.artifact_attestation.validate_receipt",
                return_value=receipt_summary,
            ):
                document = create_distribution_attestation(
                    root,
                    artifacts,
                    attestation,
                    receipt_path=receipt,
                )
                self.assertEqual(checkpoint, document["checkpoint"])
                self.assertEqual(
                    "sha256:" + hashlib.sha256(b"wheel").hexdigest(),
                    document["artifacts"][0]["sha256"],
                )
                self.assertEqual(
                    "sample-v0.2.0-evidence.json",
                    document["lifecycleReceipt"]["asset"],
                )
                self.assertEqual(
                    document,
                    validate_distribution_attestation(
                        root,
                        artifacts,
                        attestation,
                        receipt_path=receipt,
                        checkpoint=checkpoint,
                    ),
                )

                receipt_bytes = receipt.read_bytes()
                receipt.write_text('{"bounded":"changed"}\n', encoding="utf-8")
                with self.assertRaisesRegex(
                    ContractError, "does not match release, receipt, checkpoint, and bytes"
                ):
                    validate_distribution_attestation(
                        root,
                        artifacts,
                        attestation,
                        receipt_path=receipt,
                        checkpoint=checkpoint,
                    )
                receipt.write_bytes(receipt_bytes)
                (artifacts / "sample-0.2.0-py3-none-any.whl").write_bytes(b"other")
                with self.assertRaisesRegex(
                    ContractError, "does not match release, receipt, checkpoint, and bytes"
                ):
                    validate_distribution_attestation(
                        root,
                        artifacts,
                        attestation,
                        receipt_path=receipt,
                        checkpoint=checkpoint,
                    )


if __name__ == "__main__":
    unittest.main()
