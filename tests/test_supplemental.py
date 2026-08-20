import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jsonschema

import engineering_process.supplemental as supplemental
from engineering_process.contracts import ContractError


PROCESS_ROOT = Path(__file__).resolve().parent.parent
CHECKPOINT = "a" * 40
COMPARISON_BASE = "b" * 40
WORKFLOW_SHA = "c" * 40
FINGERPRINT = f"sha256:{'d' * 64}"
TIMESTAMP = "2026-08-20T00:00:00Z"


def fake_report(profile: str) -> dict[str, object]:
    check_id = f"{profile}-check"
    return {
        "schemaVersion": 2,
        "project": "engineering-process",
        "profile": profile,
        "checkpoint": CHECKPOINT,
        "workingTreeDirty": False,
        "workspaceFingerprint": FINGERPRINT,
        "completedWorkspaceFingerprint": FINGERPRINT,
        "sourceChangedDuringVerification": False,
        "impact": {
            "schemaVersion": 1,
            "mode": "full-profile",
            "profile": profile,
            "selectedCheckIds": [check_id],
            "skippedCheckIds": [],
            "checkSelection": [
                {
                    "id": check_id,
                    "selected": True,
                    "reason": "profile-has-no-impact-contract",
                    "components": [],
                    "matchedComponents": [],
                }
            ],
        },
        "startedAt": TIMESTAMP,
        "completedAt": TIMESTAMP,
        "status": "passed",
        "checks": [
            {
                "id": check_id,
                "status": "passed",
                "exitCode": 0,
                "startedAt": TIMESTAMP,
                "durationMs": 1,
                "timeoutSeconds": 30,
                "workingDirectory": ".",
                "command": ["python", "-c", "pass"],
                "commandSha256": "1" * 64,
                "impactSha256": "2" * 64,
                "impactIntegrity": "verified",
                "stdoutBytes": 0,
                "stderrBytes": 0,
                "stdoutSha256": "3" * 64,
                "stderrSha256": "4" * 64,
                "outputTruncated": False,
                "streamOutputTruncated": False,
                "pathEntries": [],
            }
        ],
    }


class SupplementalVerificationTests(unittest.TestCase):
    def build(self):
        state = {
            "checkpoint": CHECKPOINT,
            "dirty": False,
            "fingerprint": FINGERPRINT,
        }
        with (
            mock.patch.object(
                supplemental, "source_state", return_value=state
            ),
            mock.patch.object(
                supplemental, "require_environment_profile"
            ),
            mock.patch.object(
                supplemental,
                "run_profile",
                side_effect=lambda root, project, profile, base_ref: fake_report(
                    profile
                ),
            ),
        ):
            return supplemental.build_supplemental_verification(
                PROCESS_ROOT,
                expected_checkpoint=CHECKPOINT,
                comparison_base=COMPARISON_BASE,
                producer_actor="github-actions",
                producer_context="run:1:verify:Linux:python-3.14",
                provider="github-actions",
                repository="phuongnse/engineering-process",
                event_name="pull_request",
                workflow_name="CI",
                workflow_ref=(
                    "phuongnse/engineering-process/.github/workflows/ci.yml@refs/pull/3/merge"
                ),
                workflow_sha=WORKFLOW_SHA,
                run_id="12345",
                run_attempt=1,
                job="verify",
                run_url="https://github.com/phuongnse/engineering-process/actions/runs/12345/attempts/1",
                runner_os="Linux",
                runner_arch="X64",
                triggered_by="phuongnse",
            )

    def test_manifest_and_reports_are_schema_valid_and_byte_bound(self):
        manifest, reports = self.build()
        manifest_schema = json.loads(
            (
                PROCESS_ROOT
                / "schemas"
                / "supplemental-verification.schema.json"
            ).read_text(encoding="utf-8")
        )
        verification_schema = json.loads(
            (PROCESS_ROOT / "schemas" / "verification.schema.json").read_text(
                encoding="utf-8"
            )
        )

        jsonschema.Draft202012Validator(manifest_schema).validate(manifest)
        report_validator = jsonschema.Draft202012Validator(
            verification_schema
        )
        for report in reports.values():
            report_validator.validate(report)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "ci-evidence"
            written = supplemental.write_supplemental_bundle(
                PROCESS_ROOT, output, manifest, reports
            )
            self.assertEqual(output, written)
            self.assertEqual(
                {"development.json", "manifest.json", "review.json"},
                {path.name for path in output.iterdir()},
            )
            stored_manifest = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )
            for entry in stored_manifest["reports"]:
                content = (output / entry["path"]).read_bytes()
                self.assertEqual(entry["bytes"], len(content))
                self.assertEqual(
                    entry["sha256"],
                    f"sha256:{hashlib.sha256(content).hexdigest()}",
                )

    def test_timeout_metadata_is_additive_to_verification_schema_two(self):
        report = fake_report("development")
        report["checks"][0].pop("timeoutSeconds")
        schema = json.loads(
            (PROCESS_ROOT / "schemas" / "verification.schema.json").read_text(
                encoding="utf-8"
            )
        )

        jsonschema.Draft202012Validator(schema).validate(report)

    def test_checkpoint_mismatch_fails_before_running_profiles(self):
        run_profile = mock.Mock()
        state = {
            "checkpoint": "f" * 40,
            "dirty": False,
            "fingerprint": FINGERPRINT,
        }
        with (
            mock.patch.object(
                supplemental, "source_state", return_value=state
            ),
            mock.patch.object(supplemental, "run_profile", run_profile),
            self.assertRaisesRegex(ContractError, "does not match checkout HEAD"),
        ):
            supplemental.build_supplemental_verification(
                PROCESS_ROOT,
                expected_checkpoint=CHECKPOINT,
                comparison_base=COMPARISON_BASE,
                producer_actor="github-actions",
                producer_context="context",
                provider="github-actions",
                repository="phuongnse/engineering-process",
                event_name="pull_request",
                workflow_name="CI",
                workflow_ref="owner/repo/.github/workflows/ci.yml@refs/heads/main",
                workflow_sha=WORKFLOW_SHA,
                run_id="1",
                run_attempt=1,
                job="verify",
                run_url="https://github.com/owner/repo/actions/runs/1",
                runner_os="Linux",
                runner_arch="X64",
                triggered_by="owner",
            )
        run_profile.assert_not_called()

    def test_aggregate_report_limit_fails_closed(self):
        state = {
            "checkpoint": CHECKPOINT,
            "dirty": False,
            "fingerprint": FINGERPRINT,
        }
        with (
            mock.patch.object(
                supplemental, "source_state", return_value=state
            ),
            mock.patch.object(
                supplemental, "require_environment_profile"
            ),
            mock.patch.object(
                supplemental,
                "run_profile",
                side_effect=lambda root, project, profile, base_ref: fake_report(
                    profile
                ),
            ),
            mock.patch.object(
                supplemental, "MAX_SUPPLEMENTAL_REPORT_TOTAL_BYTES", 100
            ),
            self.assertRaisesRegex(ContractError, "aggregate byte limit"),
        ):
            self.build()

    def test_bundle_output_inside_checkout_is_rejected(self):
        manifest, reports = self.build()

        with self.assertRaisesRegex(ContractError, "outside the checkout"):
            supplemental.write_supplemental_bundle(
                PROCESS_ROOT,
                PROCESS_ROOT / "ci-evidence",
                manifest,
                reports,
            )

    def test_bundle_rejects_duplicate_manifest_report_paths(self):
        manifest, reports = self.build()
        manifest["reports"][1]["path"] = manifest["reports"][0]["path"]

        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(ContractError, "duplicate paths"),
        ):
            supplemental.write_supplemental_bundle(
                PROCESS_ROOT,
                Path(directory) / "ci-evidence",
                manifest,
                reports,
            )

    def test_writer_rechecks_aggregate_report_limit(self):
        manifest, reports = self.build()

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                supplemental, "MAX_SUPPLEMENTAL_REPORT_TOTAL_BYTES", 100
            ),
            self.assertRaisesRegex(ContractError, "byte limit"),
        ):
            supplemental.write_supplemental_bundle(
                PROCESS_ROOT,
                Path(directory) / "ci-evidence",
                manifest,
                reports,
            )


if __name__ == "__main__":
    unittest.main()
