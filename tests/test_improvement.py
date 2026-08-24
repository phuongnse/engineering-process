import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from engineering_process.contracts import ContractError, canonical_json_digest
from engineering_process.improvement import (
    create_improvement_reproduction,
    create_improvement_resolution,
    observe_improvement_signal,
    validate_improvement_chain,
    write_improvement_artifact,
)
from engineering_process.contracts import validate_improvement_signal
from engineering_process.runner import source_state


PROCESS_ROOT = Path(__file__).resolve().parent.parent


class ImprovementProtocolTests(unittest.TestCase):
    def example(self, name: str) -> Path:
        return PROCESS_ROOT / "examples" / f"improvement-{name}.json"

    def test_complete_chain_closes_only_after_released_consumer_reproduction(self):
        signal_only = validate_improvement_chain(self.example("signal"))
        disposed = validate_improvement_chain(
            self.example("signal"),
            self.example("disposition"),
            catalog_path=self.example("catalog"),
        )
        resolved = validate_improvement_chain(
            self.example("signal"),
            self.example("disposition"),
            self.example("resolution"),
            catalog_path=self.example("catalog"),
        )
        closed = validate_improvement_chain(
            self.example("signal"),
            self.example("disposition"),
            self.example("resolution"),
            self.example("reproduction"),
            self.example("catalog"),
        )

        self.assertEqual("signal-exported", signal_only["phase"])
        self.assertEqual("producer-disposition", disposed["phase"])
        self.assertEqual("producer-released", resolved["phase"])
        self.assertFalse(resolved["closed"])
        self.assertEqual("closed", closed["phase"])
        self.assertTrue(closed["closed"])
        self.assertIsNone(closed["nextOwner"])

    def test_chain_rejects_digest_substitution(self):
        with tempfile.TemporaryDirectory() as directory:
            disposition = json.loads(
                self.example("disposition").read_text(encoding="utf-8")
            )
            disposition["signalSha256"] = f"sha256:{'0' * 64}"
            path = Path(directory) / "disposition.json"
            path.write_text(
                json.dumps(disposition, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ContractError, "canonical signal digest"):
                validate_improvement_chain(
                    self.example("signal"),
                    path,
                    catalog_path=self.example("catalog"),
                )

    def test_disposition_requires_its_exact_catalog_snapshot(self):
        with self.assertRaisesRegex(ContractError, "exact producer catalog"):
            validate_improvement_chain(
                self.example("signal"),
                self.example("disposition"),
            )

    def test_chain_rejects_symlinked_untrusted_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            alias = Path(directory) / "signal.json"
            try:
                alias.symlink_to(self.example("signal"))
            except OSError as error:
                self.skipTest(f"symlink unavailable: {error}")

            with self.assertRaisesRegex(ContractError, "regular non-symlink"):
                validate_improvement_chain(alias)

    def test_disposition_rejects_catalog_substitution_after_triage(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = json.loads(
                self.example("catalog").read_text(encoding="utf-8")
            )
            catalog["entries"][0]["status"] = "active"
            catalog["entries"][0]["activeChangeId"] = "active-change"
            path = Path(directory) / "catalog.json"
            path.write_text(
                json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ContractError, "bind the supplied catalog"):
                validate_improvement_chain(
                    self.example("signal"),
                    self.example("disposition"),
                    catalog_path=path,
                )

    def test_duplicate_disposition_must_link_the_catalog_active_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            signal = json.loads(self.example("signal").read_text(encoding="utf-8"))
            catalog = json.loads(
                self.example("catalog").read_text(encoding="utf-8")
            )
            entry = catalog["entries"][0]
            entry["status"] = "active"
            entry["lastResolution"] = None
            entry["activeChangeId"] = "expected-active-change"
            disposition = json.loads(
                self.example("disposition").read_text(encoding="utf-8")
            )
            disposition["decision"] = "duplicate"
            disposition["recurrence"] = "duplicate"
            disposition["catalogStatus"] = "active"
            disposition["linkedChangeId"] = "different-change"
            disposition["signalSha256"] = canonical_json_digest(signal)
            disposition["catalogSha256"] = canonical_json_digest(catalog)
            catalog_path = root / "catalog.json"
            disposition_path = root / "disposition.json"
            catalog_path.write_text(json.dumps(catalog) + "\n", encoding="utf-8")
            disposition_path.write_text(
                json.dumps(disposition) + "\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(ContractError, "catalog active change"):
                validate_improvement_chain(
                    self.example("signal"),
                    disposition_path,
                    catalog_path=catalog_path,
                )

    def test_retired_invariant_cannot_be_reactivated_by_a_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = json.loads(
                self.example("catalog").read_text(encoding="utf-8")
            )
            catalog["entries"][0]["status"] = "retired"
            disposition = json.loads(
                self.example("disposition").read_text(encoding="utf-8")
            )
            disposition["catalogStatus"] = "retired"
            disposition["catalogSha256"] = canonical_json_digest(catalog)
            catalog_path = root / "catalog.json"
            disposition_path = root / "disposition.json"
            catalog_path.write_text(json.dumps(catalog) + "\n", encoding="utf-8")
            disposition_path.write_text(
                json.dumps(disposition) + "\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(ContractError, "resolved catalog invariant"):
                validate_improvement_chain(
                    self.example("signal"),
                    disposition_path,
                    catalog_path=catalog_path,
                )

    def test_producer_can_assign_a_cataloged_canonical_invariant_to_an_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            signal = json.loads(self.example("signal").read_text(encoding="utf-8"))
            signal["claim"]["proposedInvariantId"] = "windows-helper-alias"
            disposition = json.loads(
                self.example("disposition").read_text(encoding="utf-8")
            )
            disposition["signalSha256"] = canonical_json_digest(signal)
            signal_path = root / "signal.json"
            disposition_path = root / "disposition.json"
            signal_path.write_text(json.dumps(signal) + "\n", encoding="utf-8")
            disposition_path.write_text(
                json.dumps(disposition) + "\n", encoding="utf-8"
            )

            result = validate_improvement_chain(
                signal_path,
                disposition_path,
                catalog_path=self.example("catalog"),
            )

            self.assertEqual("windows-helper-alias", signal["claim"]["proposedInvariantId"])
            self.assertEqual("single-windows-helper-protocol", result["invariantId"])
            self.assertEqual("recurrence", result["recurrence"])

    def test_artifact_writer_is_exclusive_and_validates_before_write(self):
        document = json.loads(
            self.example("signal").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "signal.json"
            digest = write_improvement_artifact(
                document, output, validate_improvement_signal
            )
            self.assertTrue(digest.startswith("sha256:"))
            with self.assertRaisesRegex(ContractError, "refusing to replace"):
                write_improvement_artifact(
                    document, output, validate_improvement_signal
                )

    def test_external_observation_exports_redacted_transport_neutral_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "consumer"
            root.mkdir()
            (root / ".process").mkdir()
            (root / ".process" / "project.json").write_text(
                '{"project":"sample-consumer"}\n', encoding="utf-8"
            )
            (root / ".process" / "process.lock").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "process": {
                            "version": "0.4.0",
                            "digest": f"sha256:{'1' * 64}",
                        },
                        "skills": ["run-change"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=root, check=True
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
            source = source_state(root)
            evidence = base / "evidence.json"
            evidence.write_text(
                json.dumps(
                    {
                        "checkpoint": source["checkpoint"],
                        "workspaceFingerprint": source["fingerprint"],
                        "checks": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            output = base / "signal.json"

            result = observe_improvement_signal(
                root,
                signal_id="external-signal",
                source_repository="example/sample-consumer",
                target_project="engineering-process",
                target_repository="example/engineering-process",
                trigger_kind="external-integration",
                trigger_status="failed",
                owner_boundary="shared-process",
                reusable_class="portability-gap",
                invariant_id="portable-external-boundary",
                rationale_sha256=f"sha256:{'2' * 64}",
                affected_surfaces=["verification"],
                evidence_kind="external-event",
                evidence_path=evidence,
                reference=None,
                change_id=None,
                cycle=None,
                output=output,
            )

            signal = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("signal-exported", result["phase"])
            self.assertFalse(signal["controls"]["rawOutputIncluded"])
            self.assertFalse(signal["controls"]["environmentIncluded"])
            self.assertEqual("engineering-process", result["nextOwner"])

    def test_resolution_requires_completed_producer_receipt_and_immutable_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            signal = json.loads(self.example("signal").read_text(encoding="utf-8"))
            disposition = json.loads(
                self.example("disposition").read_text(encoding="utf-8")
            )
            completion = {
                "improvements": [
                    {
                        "role": "producer",
                        "phase": "producer-completed",
                        "invariantId": disposition["canonicalInvariantId"],
                        "signalCanonicalSha256": canonical_json_digest(signal),
                        "catalogCanonicalSha256": disposition["catalogSha256"],
                        "dispositionCanonicalSha256": canonical_json_digest(
                            disposition
                        ),
                    }
                ]
            }
            receipt = root / "receipt.json"
            receipt.write_text(
                json.dumps(
                    {
                        "artifacts": {
                            "completion": {
                                "sourceText": json.dumps(completion)
                            }
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            unrelated_receipt = root / "unrelated-receipt.json"
            unrelated_completion = json.loads(json.dumps(completion))
            unrelated_completion["improvements"][0][
                "signalCanonicalSha256"
            ] = f"sha256:{'9' * 64}"
            unrelated_receipt.write_text(
                json.dumps(
                    {
                        "artifacts": {
                            "completion": {
                                "sourceText": json.dumps(unrelated_completion)
                            }
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            release = root / "release.json"
            release.write_text("{}\n", encoding="utf-8")
            artifacts = root / "artifacts"
            artifacts.mkdir()
            attestation = root / "attestation.json"
            attestation.write_text("{}\n", encoding="utf-8")
            output = root / "resolution.json"
            with (
                mock.patch(
                    "engineering_process.evidence.validate_receipt",
                    return_value={
                        "project": "engineering-process",
                        "changeId": "close-helper-protocol-recurrence",
                        "checkpoint": "c" * 40,
                    },
                ),
                mock.patch(
                    "engineering_process.improvement.validate_release",
                    return_value=SimpleNamespace(
                        version="0.6.0", previous_version="0.5.0"
                    ),
                ),
                mock.patch(
                    "engineering_process.release.validate_release_checkpoint",
                    return_value={"version": "0.6.0"},
                ),
                mock.patch(
                    "engineering_process.artifact_attestation.validate_distribution_attestation",
                    return_value={},
                ),
            ):
                with self.assertRaisesRegex(
                    ContractError, "reviewed ingested improvement case"
                ):
                    create_improvement_resolution(
                        root,
                        self.example("signal"),
                        self.example("disposition"),
                        self.example("catalog"),
                        unrelated_receipt,
                        release,
                        None,
                        None,
                        artifacts,
                        attestation,
                        release_repository="example/engineering-process",
                        release_tag="v0.6.0",
                        release_name="v0.6.0",
                        release_commit="e" * 40,
                        regression_evidence=[f"sha256:{'1' * 64}"],
                        output=root / "unrelated-resolution.json",
                    )
                result = create_improvement_resolution(
                    root,
                    self.example("signal"),
                    self.example("disposition"),
                    self.example("catalog"),
                    receipt,
                    release,
                    None,
                    None,
                    artifacts,
                    attestation,
                    release_repository="example/engineering-process",
                    release_tag="v0.6.0",
                    release_name="v0.6.0",
                    release_commit="e" * 40,
                    regression_evidence=[f"sha256:{'1' * 64}"],
                    output=output,
                )

            self.assertEqual("producer-released", result["phase"])
            self.assertEqual("sample-consumer", result["nextOwner"])

    def test_reproduction_requires_exact_adopted_release_and_passing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "consumer"
            root.mkdir()
            (root / ".process").mkdir()
            (root / ".process" / "project.json").write_text(
                '{"project":"sample-consumer"}\n', encoding="utf-8"
            )
            (root / ".process" / "process.lock").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "process": {
                            "version": "0.6.0",
                            "digest": f"sha256:{'f' * 64}",
                        },
                        "skills": ["run-change"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=root, check=True
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "adopt release"], cwd=root, check=True)
            source = source_state(root)
            receipt = base / "receipt.json"
            receipt.write_text(
                json.dumps(
                    {
                        "artifacts": {
                            "verification": [
                                {"profile": "development"},
                                {"profile": "review"},
                            ]
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            output = base / "reproduction.json"

            with mock.patch(
                "engineering_process.evidence.validate_receipt",
                return_value={
                    "project": "sample-consumer",
                    "changeId": "adopt-process-release",
                    "cycle": 1,
                    "checkpoint": source["checkpoint"],
                    "workspaceFingerprint": source["fingerprint"],
                    "processVersion": "0.6.0",
                    "processDigest": f"sha256:{'f' * 64}",
                },
            ):
                result = create_improvement_reproduction(
                    root,
                    self.example("signal"),
                    self.example("disposition"),
                    self.example("catalog"),
                    self.example("resolution"),
                    receipt,
                    consumer_repository="example/sample-consumer",
                    reference=None,
                    output=output,
                )

            self.assertEqual("closed", result["phase"])
            self.assertTrue(result["closed"])


if __name__ == "__main__":
    unittest.main()
