import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engineering_process.contracts import CORE_QUALITY_DIMENSIONS, Check, ContractError, Project
from engineering_process import VERSION
from engineering_process.adoption import apply_adoption
from engineering_process.bootstrap import initialize_project
from engineering_process.lifecycle import (
    begin_implementation,
    finish_change,
    ingest_authority_transition_evidence,
    load_state,
    register_authority_transition,
    register_plan,
    start_change,
    start_review,
    submit_review,
    verify_change,
)
from engineering_process.evidence import export_receipt, validate_receipt
from engineering_process.runner import source_state
from engineering_process.transition import (
    _validate_action_pin_changes,
    _materialization_worktree,
    _run_target_adoption,
    create_bootstrap_adoption_consumption,
    observe_candidate_materialization,
    _validate_transition_validation_service,
    validate_registered_candidate,
    validate_transition_target_provenance,
)
from verification.resolve_transition_consumption_service import ServiceError, resolve
from verification.validate_protected_transition_callback import CallbackError, validate as validate_callback


class AuthorityTransitionTests(unittest.TestCase):
    def test_materialization_timeout_and_interrupt_fail_closed(self):
        failed = mock.Mock(
            timed_out=True,
            output_exceeded=False,
            descendants_found=False,
            cleanup_error=None,
            input_error=False,
            returncode=None,
            stdout=b"",
            stderr=b"",
        )
        with (
            mock.patch(
                "engineering_process.transition._target_authority_command",
                return_value=[sys.executable],
            ),
            mock.patch(
                "engineering_process.transition.run_bounded_process",
                return_value=failed,
            ),
            self.assertRaisesRegex(ContractError, "bounded execution boundary"),
        ):
            _run_target_adoption(
                Path("."),
                Path("."),
                action="apply",
                deadline=10**12,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Transition Test"], cwd=root, check=True)
            (root / "tracked.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            with self.assertRaises(KeyboardInterrupt):
                with _materialization_worktree(root, base):
                    raise KeyboardInterrupt
            worktrees = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(1, worktrees.count("worktree "))

    def test_protected_transition_callback_executes_exact_identity_checks(self):
        policy = json.loads(
            (
                Path(__file__).resolve().parent.parent
                / "examples"
                / "protected-transition-policy.json"
            ).read_text(encoding="utf-8")
        )
        base = "a" * 40
        head = "b" * 40
        pull_request = {
            "number": 72,
            "baseRefOid": base,
            "headRefOid": head,
            "state": "OPEN",
        }

        result = validate_callback(
            policy,
            pull_request,
            repository=policy["repository"],
            pull_request_number=72,
            source_base=base,
            candidate_head=head,
            current_base=base,
            workflow_sha=base,
            event_name="workflow_dispatch",
        )

        self.assertEqual(policy["verifier"]["commit"], result["verifierCommit"])
        with self.assertRaisesRegex(CallbackError, "exact open pull request"):
            validate_callback(
                policy,
                {**pull_request, "headRefOid": "c" * 40},
                repository=policy["repository"],
                pull_request_number=72,
                source_base=base,
                candidate_head=head,
                current_base=base,
                workflow_sha=base,
                event_name="workflow_dispatch",
            )

    def test_validation_and_consumption_service_chain_is_executable(self):
        policy = json.loads(
            (
                Path(__file__).resolve().parent.parent
                / "examples"
                / "protected-transition-policy.json"
            ).read_text(encoding="utf-8")
        )
        base = "a" * 40
        head = "b" * 40
        service = _validate_transition_validation_service(
            {
                "schemaVersion": 1,
                "kind": "engineering-process-transition-validation-service",
                "repository": policy["repository"],
                "workflowPath": policy["workflow"]["path"],
                "workflowSha": base,
                "runId": "456",
                "runAttempt": 1,
                "runUrl": "https://github.com/owner/engineering-process/actions/runs/456/attempts/1",
                "event": "workflow_dispatch",
                "headSha": base,
                "checkContext": policy["workflow"]["checkContext"],
                "checkAppId": policy["workflow"]["checkAppId"],
            },
            policy=policy,
            protected_base=base,
        )
        validation = {
            "headCheckpoint": head,
            "validationService": service,
        }
        run = {
            "id": 456,
            "run_attempt": 1,
            "repository": {"full_name": service["repository"]},
            "path": service["workflowPath"],
            "head_sha": base,
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
        }
        checks = {
            "check_runs": [
                {
                    "id": 789,
                    "name": service["checkContext"],
                    "head_sha": head,
                    "conclusion": "success",
                    "app": {"id": service["checkAppId"]},
                }
            ]
        }

        observed = resolve(validation, run, checks)

        self.assertEqual("789", observed["checkRunId"])
        checks["check_runs"][0]["app"]["id"] += 1
        with self.assertRaisesRegex(ServiceError, "exactly one"):
            resolve(validation, run, checks)

    def test_materialization_observes_apply_check_idempotence_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "project.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "project": "consumer",
                        "lifecycle": {"requiredProfiles": ["development"]},
                        "profiles": {
                            "development": [
                                {
                                    "id": "unit",
                                    "run": ["python", "-c", "raise SystemExit(0)"],
                                    "timeoutSeconds": 30,
                                }
                            ]
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            initialize_project(
                root,
                Path(__file__).resolve().parent.parent,
                manifest_path=manifest,
                requested_bundles=["docs"],
                replace=False,
            )
            requirements = root / "requirements"
            requirements.mkdir()
            (requirements / "process.in").write_text(
                f"engineering-process=={VERSION}\n", encoding="utf-8"
            )
            (requirements / "process.txt").write_text(
                "--only-binary :all:\n"
                f"engineering-process=={VERSION} \\\n"
                f"    --hash=sha256:{'0' * 64}\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Transition Test"], cwd=root, check=True)
            lock_path = root / ".process" / "process.lock"
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            lock["process"] = {
                "version": "0.1.0",
                "digest": f"sha256:{'0' * 64}",
            }
            lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()

            apply_adoption(
                root,
                Path(__file__).resolve().parent.parent,
                requirements / "process.txt",
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "candidate"], cwd=root, check=True)
            tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            changed = subprocess.run(["git", "diff", "--name-only", base, "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.splitlines()
            target_lock = json.loads(lock_path.read_text(encoding="utf-8"))
            request = {
                "target": {
                    "version": VERSION,
                    "processDigest": target_lock["process"]["digest"],
                },
                "candidate": {
                    "baseCheckpoint": base,
                    "expectedChangedPaths": changed,
                    "actionPins": [],
                    "projectMigrationSha256": None,
                },
            }

            observed = observe_candidate_materialization(
                root,
                request,
                target_process_root=Path(__file__).resolve().parent.parent,
                expected_tree=tree,
            )

            self.assertEqual(tree, observed["applyTree"])
            self.assertEqual(tree, observed["idempotentTree"])
            self.assertEqual(
                observed["rollback"]["beforeTree"],
                observed["rollback"]["afterTree"],
            )

    def test_target_provenance_resolves_exact_release_bytes(self):
        request = json.loads((Path(__file__).resolve().parent.parent / "examples" / "authority-transition-request.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "target"
            artifacts = root / "artifacts"
            checkout.mkdir()
            artifacts.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://github.com/owner/engineering-process.git"],
                cwd=checkout,
                check=True,
            )
            (checkout / "release.json").write_text("{}\n", encoding="utf-8")
            receipt = root / "receipt.json"
            receipt.write_text("{}\n", encoding="utf-8")
            attestation = root / "attestation.json"
            attestation.write_text("{}\n", encoding="utf-8")
            digest = lambda path: f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
            request["target"]["releaseContractSha256"] = digest(checkout / "release.json")
            request["target"]["lifecycleReceiptSha256"] = digest(receipt)
            request["target"]["distributionAttestationSha256"] = digest(attestation)
            release_result = {
                "version": request["target"]["version"],
                "provenanceMode": "governed",
                "lifecycleReceipt": {
                    "processVersion": request["source"]["authority"]["version"],
                    "processDigest": request["source"]["authority"]["digest"],
                },
            }
            attestation_document = {"artifacts": request["target"]["artifacts"]}
            with mock.patch("engineering_process.release.validate_release_checkpoint", return_value=release_result) as release_check, mock.patch("engineering_process.artifact_attestation.validate_distribution_attestation", return_value=attestation_document) as attestation_check:
                result = validate_transition_target_provenance(
                    request,
                    target_checkout=checkout,
                    artifact_root=artifacts,
                    release_receipt_path=receipt,
                    artifact_attestation_path=attestation,
                )
            self.assertEqual(request["target"]["commit"], result["commit"])
            release_check.assert_called_once()
            attestation_check.assert_called_once()
            subprocess.run(
                ["git", "remote", "set-url", "origin", "https://github.com/attacker/engineering-process.git"],
                cwd=checkout,
                check=True,
            )
            with self.assertRaisesRegex(ContractError, "repository mismatch"):
                validate_transition_target_provenance(
                    request,
                    target_checkout=checkout,
                    artifact_root=artifacts,
                    release_receipt_path=receipt,
                    artifact_attestation_path=attestation,
                )
            subprocess.run(
                ["git", "remote", "set-url", "origin", "git@github.com:owner/engineering-process.git"],
                cwd=checkout,
                check=True,
            )
            receipt.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "provenance mismatch"):
                with mock.patch("engineering_process.release.validate_release_checkpoint", return_value=release_result):
                    validate_transition_target_provenance(
                        request,
                        target_checkout=checkout,
                        artifact_root=artifacts,
                        release_receipt_path=receipt,
                        artifact_attestation_path=attestation,
                    )

    def test_expired_request_is_rejected_before_registration(self):
        request = json.loads((Path(__file__).resolve().parent.parent / "examples" / "authority-transition-request.json").read_text(encoding="utf-8"))
        request["candidate"]["expiresAt"] = "2020-01-01T00:00:00Z"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ContractError, "expired"):
                validate_transition_target_provenance(
                    request,
                    target_checkout=root,
                    artifact_root=root,
                    release_receipt_path=root / "receipt.json",
                    artifact_attestation_path=root / "attestation.json",
                )

    def test_action_pin_changes_are_grouped_and_pin_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Transition Test"], cwd=root, check=True)
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            workflow = workflows / "ci.yml"
            previous = "b" * 40
            target = "c" * 40
            workflow.write_bytes(
                (
                    "steps:\r\n  - uses: actions/checkout@"
                    + "a" * 40
                    + " # v1.0.0\r\n  - uses: owner/process@"
                    + previous
                    + " # v0.7.0\r\n"
                ).encode("utf-8")
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            workflow.write_bytes(
                workflow.read_bytes().replace(
                    (previous + " # v0.7.0").encode("utf-8"),
                    (target + " # v0.9.0").encode("utf-8"),
                )
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "pin update"], cwd=root, check=True)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            declarations = [{"path": ".github/workflows/ci.yml", "repository": "owner/process", "previousCommit": previous, "targetCommit": target, "previousReleaseTag": "v0.7.0", "targetReleaseTag": "v0.9.0"}]

            digest = _validate_action_pin_changes(root, base_checkpoint=base, head_checkpoint=head, declarations=declarations)

            self.assertTrue(digest.startswith("sha256:"))
            with self.assertRaisesRegex(ContractError, "do not match declared"):
                _validate_action_pin_changes(root, base_checkpoint=base, head_checkpoint=head, declarations=[])
            workflow.write_bytes(workflow.read_bytes() + b"  - run: echo unrelated\r\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "unrelated"], cwd=root, check=True)
            unrelated = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            with self.assertRaisesRegex(ContractError, "beyond declared pins"):
                _validate_action_pin_changes(root, base_checkpoint=base, head_checkpoint=unrelated, declarations=declarations)

    def test_consumption_binds_merge_tree_and_validation_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Transition Test"], cwd=root, check=True)
            (root / "tracked.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            (root / "tracked.txt").write_text("merged\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "merge"], cwd=root, check=True)
            merge = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            validation = {
                "project": "engineering-process", "repository": "owner/engineering-process",
                "policySha256": f"sha256:{'1' * 64}", "intentSha256": f"sha256:{'2' * 64}",
                "authorizationSha256": f"sha256:{'3' * 64}", "baseCheckpoint": base,
                "headCheckpoint": "1" * 40, "headTree": tree, "checkContext": "authority-transition-completion",
            }
            validation["validationService"] = {
                "schemaVersion": 1,
                "kind": "engineering-process-transition-validation-service",
                "repository": validation["repository"],
                "workflowPath": ".github/workflows/authority-transition.yml",
                "workflowSha": base,
                "runId": "456",
                "runAttempt": 1,
                "runUrl": "https://github.com/owner/engineering-process/actions/runs/456/attempts/1",
                "event": "workflow_dispatch",
                "headSha": base,
                "checkContext": validation["checkContext"],
                "checkAppId": 12345,
            }
            service = {"artifactId": "123", "name": f"authority-transition-validation-{validation['headCheckpoint']}", "digest": f"sha256:{'4' * 64}", "runId": "456", "runAttempt": 1, "runUrl": "https://github.com/owner/engineering-process/actions/runs/456/attempts/1"}
            observed_service = {
                **validation["validationService"],
                "schemaVersion": 1,
                "kind": "engineering-process-transition-consumption-service",
                "runStatus": "completed",
                "runConclusion": "success",
                "checkRunId": "789",
                "checkHeadSha": validation["headCheckpoint"],
                "checkConclusion": "success",
            }

            consumption = create_bootstrap_adoption_consumption(root, validation, merge_checkpoint=merge, validation_artifact=service, validation_service=observed_service, consumed_at="2026-01-03T00:00:00Z")

            self.assertEqual(service, consumption["validationArtifact"])
            self.assertEqual(observed_service, consumption["validationService"])
            tampered_service = dict(observed_service)
            tampered_service["checkAppId"] = 54321
            with self.assertRaisesRegex(ContractError, "does not match validation service"):
                create_bootstrap_adoption_consumption(root, validation, merge_checkpoint=merge, validation_artifact=service, validation_service=tampered_service, consumed_at="2026-01-03T00:00:00Z")
            validation["headTree"] = "f" * 40
            with self.assertRaisesRegex(ContractError, "merge tree"):
                create_bootstrap_adoption_consumption(root, validation, merge_checkpoint=merge, validation_artifact=service, validation_service=observed_service, consumed_at="2026-01-03T00:00:00Z")

    def project(self) -> Project:
        check = Check(
            identifier="unit",
            run=(sys.executable, "-c", "raise SystemExit(0)"),
            timeout_seconds=10,
            working_directory=".",
        )
        return Project(
            identifier="sample-project",
            profiles={"development": (check,)},
            required_profiles=("development",),
        )

    def initialize(self, root: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Transition Test"], cwd=root, check=True)
        (root / ".gitignore").write_text(".process/runs/\n", encoding="utf-8")
        (root / ".process").mkdir()
        (root / "requirements").mkdir()
        (root / ".process" / "process.lock").write_text(
            json.dumps({"schemaVersion": 1, "process": {"version": "0.7.0", "digest": f"sha256:{'0' * 64}"}, "skills": ["run-change"]}) + "\n",
            encoding="utf-8",
        )
        (root / ".process" / "project.json").write_text(
            json.dumps({
                "schemaVersion": 2,
                "project": "sample-project",
                "lifecycle": {"requiredProfiles": ["development"]},
                "profiles": {"development": [{"id": "unit", "run": ["python", "-c", "raise SystemExit(0)"], "timeoutSeconds": 10}]},
                "environment": {
                    "defaultProfile": "development",
                    "managedTools": [],
                    "profiles": {"development": ["python-runtime"]},
                    "requirements": [{"id": "python-runtime", "description": "Python", "probe": {"run": ["python", "--version"], "timeoutSeconds": 10, "readOnly": True}, "remediation": "Install Python"}],
                    "setupActions": [],
                },
            }) + "\n",
            encoding="utf-8",
        )
        (root / "requirements" / "process.txt").write_text("engineering-process==0.7.0\n", encoding="utf-8")
        (root / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)

    def write_contract_and_plan(self, root: Path) -> tuple[Path, Path]:
        inputs = root / ".process" / "runs" / "_inputs"
        inputs.mkdir(parents=True, exist_ok=True)
        contract = inputs / "contract.json"
        contract.write_text(json.dumps({
            "schemaVersion": 3,
            "id": "adopt-process-0-9-0",
            "summary": "Adopt the target process",
            "source": "test",
            "comparisonBase": "HEAD",
            "specification": {"kind": "change-contract", "reference": "test", "rationale": "Fixture"},
            "risk": "high",
            "affectedProjects": ["sample-project"],
            "acceptanceCriteria": [{"id": "ac-1", "outcome": "Adoption is governed"}],
            "requiredProfiles": ["development"],
            "quality": {
                "standard": "production-v1",
                "assessments": [
                    {"dimension": dimension, "status": "applicable", "rationale": "Transition fixture", "criteria": ["ac-1"]}
                    for dimension in CORE_QUALITY_DIMENSIONS
                ],
            },
            "signOff": {"required": False, "status": "not-required", "evidence": None},
        }) + "\n", encoding="utf-8")
        state = start_change(root, self.project(), contract, actor_id="worker", context_id="ctx", kind="agent")
        plan = inputs / "plan.json"
        plan.write_text(json.dumps({
            "schemaVersion": 2,
            "changeId": "adopt-process-0-9-0",
            "contractDigest": state["contract"]["digest"],
            "approach": "Use the explicit transition route.",
            "workItems": [{"id": "work-1", "outcome": "Adopt", "affectedPaths": [".process/process.lock"], "verificationProfiles": ["development"]}],
            "acceptancePlan": [{"criterionId": "ac-1", "workItems": ["work-1"], "verificationProfiles": ["development"]}],
            "risks": [],
            "openDecisions": [],
        }) + "\n", encoding="utf-8")
        register_plan(root, self.project(), "adopt-process-0-9-0", plan, actor_id="worker", context_id="ctx", kind="agent")
        begin_implementation(root, "adopt-process-0-9-0", actor_id="worker", context_id="ctx", kind="agent")
        return contract, plan

    def test_registers_transition_before_candidate_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.initialize(root)
            self.write_contract_and_plan(root)
            source = source_state(root)
            digest = lambda path: f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
            request = json.loads((Path(__file__).resolve().parent.parent / "examples" / "authority-transition-request.json").read_text(encoding="utf-8"))
            request["project"] = "sample-project"
            request["changeId"] = "adopt-process-0-9-0"
            request["source"] = {
                "authority": {"version": "0.7.0", "digest": f"sha256:{'0' * 64}"},
                "checkpoint": source["checkpoint"],
                "workspaceFingerprint": source["fingerprint"],
                "processLockSha256": digest(root / ".process" / "process.lock"),
                "requirementsLockSha256": digest(root / "requirements" / "process.txt"),
            }
            request["candidate"]["baseCheckpoint"] = source["checkpoint"]
            request_path = root / ".process" / "runs" / "_inputs" / "request.json"
            request_path.write_text(json.dumps(request) + "\n", encoding="utf-8")

            with mock.patch("engineering_process.lifecycle.validate_transition_target_provenance", return_value={}):
                state, _ = register_authority_transition(root, "adopt-process-0-9-0", request_path, root, root, request_path, request_path, actor_id="worker", context_id="ctx", kind="agent")

            self.assertEqual(3, state["schemaVersion"])
            self.assertIsNone(state["authorityTransition"]["candidateEvidence"])
            self.assertEqual(3, load_state(root, "adopt-process-0-9-0")["schemaVersion"])
            with self.assertRaisesRegex(ContractError, "already registered"):
                register_authority_transition(root, "adopt-process-0-9-0", request_path, root, root, request_path, request_path, actor_id="worker", context_id="ctx", kind="agent")

    def test_candidate_evidence_is_recomputed_not_trusted(self):
        request = json.loads((Path(__file__).resolve().parent.parent / "examples" / "authority-transition-request.json").read_text(encoding="utf-8"))
        evidence = json.loads((Path(__file__).resolve().parent.parent / "examples" / "authority-transition-evidence.json").read_text(encoding="utf-8"))
        from engineering_process.contracts import canonical_json_digest
        evidence["requestSha256"] = canonical_json_digest(request)
        evidence["project"] = request["project"]
        evidence["changeId"] = request["changeId"]
        evidence["cycle"] = request["cycle"]
        evidence["sourceAuthority"] = request["source"]["authority"]
        evidence["targetAuthority"] = {"version": request["target"]["version"], "digest": request["target"]["processDigest"]}
        inspected = {
            "checkpoint": evidence["candidate"]["checkpoint"],
            "tree": evidence["candidate"]["tree"],
            "workspaceFingerprint": evidence["candidate"]["workspaceFingerprint"],
            "changedPaths": evidence["candidate"]["changedPaths"],
            "lock": mock.Mock(skills=tuple(request["candidate"]["selectedSkills"])),
        }
        for field in ("requirementsInputSha256", "requirementsLockSha256", "processLockSha256", "projectManifestSha256"):
            evidence["bindings"][field] = f"sha256:{'a' * 64}"
        for field in ("requirementsInputSha256", "requirementsLockSha256", "projectManifestSha256"):
            request["candidate"][field] = evidence["bindings"][field]
        request["candidate"]["actionPinsSha256"] = evidence["bindings"]["actionPinsSha256"]
        evidence["bindings"]["managedDistributionSha256"] = request["target"]["processDigest"]
        evidence["requestSha256"] = canonical_json_digest(request)
        with mock.patch("engineering_process.transition.inspect_transition_candidate", return_value=inspected), mock.patch("engineering_process.transition.observe_candidate_materialization", return_value=evidence["materialization"]), mock.patch("engineering_process.transition._file_digest", return_value=f"sha256:{'a' * 64}"), mock.patch("engineering_process.transition._validate_action_pin_changes", return_value=evidence["bindings"]["actionPinsSha256"]):
            validate_registered_candidate(Path("."), request, evidence, target_process_root=Path("."))
            evidence["candidate"]["tree"] = "f" * 40
            with self.assertRaisesRegex(ContractError, "stale|apply.*tree"):
                validate_registered_candidate(Path("."), request, evidence, target_process_root=Path("."))

    def test_n_minus_one_completes_exact_external_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "control"
            root.mkdir()
            self.initialize(root)
            self.write_contract_and_plan(root)
            source = source_state(root)
            digest = lambda path: f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
            request = json.loads((Path(__file__).resolve().parent.parent / "examples" / "authority-transition-request.json").read_text(encoding="utf-8"))
            request["project"] = "sample-project"
            request["changeId"] = "adopt-process-0-9-0"
            request["source"] = {
                "authority": {"version": "0.7.0", "digest": f"sha256:{'0' * 64}"},
                "checkpoint": source["checkpoint"],
                "workspaceFingerprint": source["fingerprint"],
                "processLockSha256": digest(root / ".process" / "process.lock"),
                "requirementsLockSha256": digest(root / "requirements" / "process.txt"),
            }
            request["candidate"]["baseCheckpoint"] = source["checkpoint"]
            request["candidate"]["expectedChangedPaths"] = [".process/process.lock", "tracked.txt"]
            request_path = root / ".process" / "runs" / "_inputs" / "request.json"
            request_path.write_text(json.dumps(request) + "\n", encoding="utf-8")
            with mock.patch("engineering_process.lifecycle.validate_transition_target_provenance", return_value={}):
                register_authority_transition(root, "adopt-process-0-9-0", request_path, root, root, request_path, request_path, actor_id="worker", context_id="ctx", kind="agent")

            candidate = Path(directory) / "candidate"
            subprocess.run(["git", "clone", "-q", str(root), str(candidate)], check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=candidate, check=True)
            subprocess.run(["git", "config", "user.name", "Transition Test"], cwd=candidate, check=True)
            (candidate / "tracked.txt").write_text("candidate\n", encoding="utf-8")
            lock = json.loads((candidate / ".process" / "process.lock").read_text(encoding="utf-8"))
            lock["process"] = {"version": "0.9.0", "digest": f"sha256:{'1' * 64}"}
            (candidate / ".process" / "process.lock").write_text(json.dumps(lock) + "\n", encoding="utf-8")
            subprocess.run(["git", "add", ".process/process.lock", "tracked.txt"], cwd=candidate, check=True)
            subprocess.run(["git", "commit", "-qm", "candidate"], cwd=candidate, check=True)
            candidate_source = source_state(candidate)
            tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=candidate, check=True, capture_output=True, text=True).stdout.strip()
            from engineering_process.contracts import canonical_json_digest
            evidence = json.loads((Path(__file__).resolve().parent.parent / "examples" / "authority-transition-evidence.json").read_text(encoding="utf-8"))
            evidence.update({
                "project": request["project"],
                "changeId": request["changeId"],
                "cycle": request["cycle"],
                "requestSha256": canonical_json_digest(request),
                "sourceAuthority": request["source"]["authority"],
                "targetAuthority": {"version": "0.9.0", "digest": f"sha256:{'1' * 64}"},
            })
            evidence["candidate"] = {
                "baseCheckpoint": source["checkpoint"],
                "checkpoint": candidate_source["checkpoint"],
                "tree": tree,
                "workspaceFingerprint": candidate_source["fingerprint"],
                "workingTreeDirty": False,
                "changedPaths": request["candidate"]["expectedChangedPaths"],
            }
            evidence["materialization"]["applyTree"] = tree
            evidence["materialization"]["idempotentTree"] = tree
            evidence_path = root / ".process" / "runs" / "_inputs" / "evidence.json"
            evidence_path.write_text(json.dumps(evidence) + "\n", encoding="utf-8")
            validated = {"evidence": evidence, "candidate": {"checkpoint": candidate_source["checkpoint"], "tree": tree}}
            (root / "tracked.txt").write_text("dirty-before-ingest\n", encoding="utf-8")
            with (
                mock.patch(
                    "engineering_process.lifecycle.validate_registered_candidate",
                    return_value=validated,
                ),
                self.assertRaisesRegex(ContractError, "control workspace"),
            ):
                ingest_authority_transition_evidence(root, "adopt-process-0-9-0", candidate, evidence_path, Path(__file__).resolve().parent.parent, actor_id="worker", context_id="ctx", kind="agent")
            self.assertIsNone(
                load_state(root, "adopt-process-0-9-0")["authorityTransition"]["candidateEvidence"]
            )
            (root / "tracked.txt").write_text("base\n", encoding="utf-8")
            with mock.patch("engineering_process.lifecycle.validate_registered_candidate", return_value=validated):
                ingest_authority_transition_evidence(root, "adopt-process-0-9-0", candidate, evidence_path, Path(__file__).resolve().parent.parent, actor_id="worker", context_id="ctx", kind="agent")

            (root / "tracked.txt").write_text("dirty-control\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "control workspace"):
                verify_change(root, self.project(), "adopt-process-0-9-0", "development", candidate_root=candidate, actor_id="worker", context_id="ctx", kind="agent")
            (root / "tracked.txt").write_text("base\n", encoding="utf-8")

            state, report = verify_change(root, self.project(), "adopt-process-0-9-0", "development", candidate_root=candidate, actor_id="worker", context_id="ctx", kind="agent")
            self.assertEqual(4, report["schemaVersion"])
            self.assertEqual("verified", state["phase"])
            state, assignment = start_review(root, "adopt-process-0-9-0", candidate_root=candidate, actor_id="reviewer", context_id="review-context", kind="agent", method="isolated-context", attested_by="host", evidence="Fresh read-only context")
            review = {
                "schemaVersion": 4,
                "changeId": "adopt-process-0-9-0",
                "cycle": 1,
                "checkpoint": assignment["checkpoint"],
                "workspaceFingerprint": assignment["workspaceFingerprint"],
                "comparisonBase": assignment["comparisonBase"],
                "reviewer": assignment["reviewer"],
                "independence": assignment["independence"],
                "verdict": "approved",
                "quality": {"standard": "production-v1", "assessments": [{"dimension": dimension, "status": "verified", "criteria": ["ac-1"], "evidence": "Verified exact candidate"} for dimension in CORE_QUALITY_DIMENSIONS]},
                "authorityTransition": state["authorityTransition"],
                "findings": [],
            }
            review_path = root / ".process" / "runs" / "_inputs" / "review.json"
            review_path.write_text(json.dumps(review) + "\n", encoding="utf-8")
            state = submit_review(root, "adopt-process-0-9-0", review_path, candidate_root=candidate)
            self.assertEqual("approved", state["phase"])
            state, completion = finish_change(root, "adopt-process-0-9-0", candidate_root=candidate, actor_id="worker", context_id="ctx", kind="agent")
            self.assertEqual(2, completion["schemaVersion"])
            self.assertEqual("completed", state["phase"])
            receipt_path = Path(directory) / "transition-receipt.json"
            receipt = export_receipt(root, "adopt-process-0-9-0", receipt_path)
            self.assertEqual(candidate_source["checkpoint"], receipt["checkpoint"])
            self.assertEqual(receipt, validate_receipt(receipt_path))


if __name__ == "__main__":
    unittest.main()
