import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engineering_process.contracts import CORE_QUALITY_DIMENSIONS, Check, ContractError, Project
from engineering_process.cli import main as cli_main
from engineering_process.contracts import canonical_json_digest
from engineering_process import VERSION
from engineering_process.adoption import apply_adoption
from engineering_process.bootstrap import initialize_project
from engineering_process.lifecycle import (
    begin_implementation,
    _resolve_target_repository_proof,
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
from verification.validate_transition_check_exclusivity import (
    ExclusivityError,
    validate_exclusivity,
)
from verification.build_target_repository_proof import build as build_repository_proof
from verification.fetch_target_repository_proof import (
    _fetch as fetch_repository_document,
    fetch as fetch_repository_proof,
)


class AuthorityTransitionTests(unittest.TestCase):
    def test_repository_provider_fetch_rejects_redirects_and_oversized_responses(self):
        url = "https://api.github.com/repos/owner/engineering-process"
        redirected = mock.MagicMock()
        redirected.__enter__.return_value.geturl.return_value = url + "/redirected"
        redirected.__enter__.return_value.status = 200
        with (
            mock.patch(
                "verification.fetch_target_repository_proof.urlopen",
                return_value=redirected,
            ),
            self.assertRaisesRegex(ContractError, "redirected"),
        ):
            fetch_repository_document(url)

        oversized = mock.MagicMock()
        oversized.__enter__.return_value.geturl.return_value = url
        oversized.__enter__.return_value.status = 200
        oversized.__enter__.return_value.read.return_value = b"x" * 2_000_001
        with (
            mock.patch(
                "verification.fetch_target_repository_proof.urlopen",
                return_value=oversized,
            ),
            self.assertRaisesRegex(ContractError, "exceeds"),
        ):
            fetch_repository_document(url)

    def test_registration_accepts_only_nonce_bound_fixed_adapter_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "verification").mkdir()
            adapter = root / "verification" / "fetch_target_repository_proof.py"
            adapter.write_text("# fixed adapter\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Transition Test"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "adapter"], cwd=root, check=True)
            checkpoint = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            request = json.loads(
                (
                    Path(__file__).resolve().parent.parent
                    / "examples"
                    / "authority-transition-request.json"
                ).read_text(encoding="utf-8")
            )
            request["source"]["checkpoint"] = checkpoint
            target = request["target"]
            proof = {
                "schemaVersion": 1,
                "kind": "engineering-process-target-repository-proof",
                "provider": "github",
                "repository": target["repository"],
                "repositoryId": "123",
                "repositoryUrl": f"https://api.github.com/repos/{target['repository']}",
                "releaseId": "456",
                "releaseUrl": f"https://api.github.com/repos/{target['repository']}/releases/456",
                "tag": target["tag"],
                "commit": target["commit"],
                "immutable": True,
                "assets": [
                    {
                        "artifactId": str(700 + index),
                        "name": item["name"],
                        "url": f"https://api.github.com/repos/{target['repository']}/releases/assets/{700 + index}",
                        "sizeBytes": item["sizeBytes"],
                        "sha256": item["sha256"],
                    }
                    for index, item in enumerate(target["artifacts"])
                ],
            }
            request["target"]["repositoryProofSha256"] = canonical_json_digest(proof)

            def adapter_result(command, **_kwargs):
                nonce = command[command.index("--nonce") + 1]
                output = Path(command[command.index("--output") + 1])
                output.write_text(
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "kind": "engineering-process-target-repository-proof-envelope",
                            "nonce": nonce,
                            "proof": proof,
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return mock.Mock(
                    returncode=0,
                    stdout=b'{"status":"passed"}\n',
                    stderr=b"",
                    timed_out=False,
                    output_exceeded=False,
                    descendants_found=False,
                    cleanup_error=None,
                    input_error=False,
                )

            with mock.patch(
                "engineering_process.lifecycle.run_bounded_process",
                side_effect=adapter_result,
            ):
                observed = _resolve_target_repository_proof(
                    root, request
                )
            self.assertEqual(proof, observed)

            adapter.unlink()
            with self.assertRaisesRegex(ContractError, "unavailable"):
                _resolve_target_repository_proof(root, request)
            adapter.write_text("# fixed adapter\n", encoding="utf-8")

            adapter.write_text("# changed adapter\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "not fixed"):
                _resolve_target_repository_proof(root, request)
            adapter.write_text("# fixed adapter\n", encoding="utf-8")

            timed_out = mock.Mock(
                returncode=None,
                stdout=b"",
                stderr=b"",
                timed_out=True,
                output_exceeded=False,
                descendants_found=False,
                cleanup_error=None,
                input_error=False,
            )
            with (
                mock.patch(
                    "engineering_process.lifecycle.run_bounded_process",
                    return_value=timed_out,
                ),
                self.assertRaisesRegex(ContractError, "bounded execution"),
            ):
                _resolve_target_repository_proof(root, request)

            def forged_result(command, **kwargs):
                result = adapter_result(command, **kwargs)
                output = Path(command[command.index("--output") + 1])
                envelope = json.loads(output.read_text(encoding="utf-8"))
                envelope["nonce"] = "0" * 64
                output.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
                return result

            with (
                mock.patch(
                    "engineering_process.lifecycle.run_bounded_process",
                    side_effect=forged_result,
                ),
                self.assertRaisesRegex(ContractError, "nonce envelope"),
            ):
                _resolve_target_repository_proof(root, request)

    def test_target_repository_proof_uses_provider_service_identity(self):
        identity = json.loads(
            (
                Path(__file__).resolve().parent.parent
                / "examples"
                / "authority-transition-request.json"
            ).read_text(encoding="utf-8")
        )
        target = identity["target"]
        repository = {
            "id": 123,
            "full_name": target["repository"],
            "url": f"https://api.github.com/repos/{target['repository']}",
        }
        release = {
            "id": 456,
            "url": f"{repository['url']}/releases/456",
            "tag_name": target["tag"],
            "immutable": True,
            "assets": [
                {
                    "id": 700 + index,
                    "name": item["name"],
                    "url": f"{repository['url']}/releases/assets/{700 + index}",
                    "size": item["sizeBytes"],
                    "digest": item["sha256"],
                }
                for index, item in enumerate(target["artifacts"])
            ],
        }
        release["assets"].extend(
            [
                {
                    "id": 900,
                    "name": "engineering-process-v0.9.0-artifacts.json",
                    "url": f"{repository['url']}/releases/assets/900",
                    "size": 1_633,
                    "digest": f"sha256:{'a' * 64}",
                },
                {
                    "id": 901,
                    "name": "engineering-process-v0.9.0-evidence.json",
                    "url": f"{repository['url']}/releases/assets/901",
                    "size": 44_349,
                    "digest": f"sha256:{'b' * 64}",
                },
            ]
        )
        tag_ref = {
            "ref": f"refs/tags/{target['tag']}",
            "object": {"type": "commit", "sha": target["commit"]},
        }

        proof = build_repository_proof(
            identity, repository, release, tag_ref, None
        )

        self.assertEqual(target["repository"], proof["repository"])
        self.assertEqual(target["commit"], proof["commit"])
        self.assertEqual(
            [item["name"] for item in target["artifacts"]],
            [item["name"] for item in proof["assets"]],
        )
        self.assertNotIn(
            "engineering-process-v0.9.0-evidence.json",
            [item["name"] for item in proof["assets"]],
        )
        with self.assertRaisesRegex(ContractError, "repository"):
            build_repository_proof(
                identity,
                {**repository, "full_name": "attacker/engineering-process"},
                release,
                tag_ref,
                None,
            )

        wrong_release = json.loads(json.dumps(release))
        wrong_release["tag_name"] = "v9.9.9"
        with self.assertRaisesRegex(ContractError, "release"):
            build_repository_proof(
                identity, repository, wrong_release, tag_ref, None
            )
        wrong_ref = json.loads(json.dumps(tag_ref))
        wrong_ref["object"]["sha"] = "f" * 40
        with self.assertRaisesRegex(ContractError, "target commit"):
            build_repository_proof(
                identity, repository, release, wrong_ref, None
            )
        wrong_assets = json.loads(json.dumps(release))
        wrong_assets["assets"][0]["digest"] = f"sha256:{'f' * 64}"
        with self.assertRaisesRegex(ContractError, "registered target"):
            build_repository_proof(
                identity, repository, wrong_assets, tag_ref, None
            )
        wrong_size = json.loads(json.dumps(release))
        wrong_size["assets"][0]["size"] += 1
        with self.assertRaisesRegex(ContractError, "registered target"):
            build_repository_proof(
                identity, repository, wrong_size, tag_ref, None
            )
        wrong_url = json.loads(json.dumps(release))
        wrong_url["assets"][0]["url"] = (
            f"{repository['url']}/releases/assets/999"
        )
        with self.assertRaisesRegex(ContractError, "does not bind"):
            build_repository_proof(
                identity, repository, wrong_url, tag_ref, None
            )
        missing_target = json.loads(json.dumps(release))
        missing_target["assets"] = missing_target["assets"][1:]
        with self.assertRaisesRegex(ContractError, "missing registered target"):
            build_repository_proof(
                identity, repository, missing_target, tag_ref, None
            )
        duplicate_target = json.loads(json.dumps(release))
        duplicate = json.loads(json.dumps(duplicate_target["assets"][0]))
        duplicate["id"] = 999
        duplicate["url"] = f"{repository['url']}/releases/assets/999"
        duplicate_target["assets"].append(duplicate)
        with self.assertRaisesRegex(ContractError, "names must be unique"):
            build_repository_proof(
                identity, repository, duplicate_target, tag_ref, None
            )
        malformed = json.loads(json.dumps(release))
        malformed["assets"].append({"name": "malformed"})
        with self.assertRaisesRegex(ContractError, "missing field"):
            build_repository_proof(
                identity, repository, malformed, tag_ref, None
            )

        service_documents = {
            repository["url"]: repository,
            f"{repository['url']}/releases/tags/{target['tag']}": release,
            f"{repository['url']}/git/ref/tags/{target['tag']}": tag_ref,
        }
        with mock.patch(
            "verification.fetch_target_repository_proof._fetch",
            side_effect=lambda url: service_documents[url],
        ) as provider_fetch:
            fetched = fetch_repository_proof(identity)
        self.assertEqual(proof, fetched)
        self.assertEqual(3, provider_fetch.call_count)

    def test_consumption_check_pagination_and_replay_are_exclusive(self):
        context = "authority-transition-consumption"
        app_id = 12345
        head = "a" * 40
        first_page = {
            "check_runs": [
                {
                    "id": index,
                    "name": f"unrelated-{index}",
                    "head_sha": head,
                    "status": "completed",
                    "conclusion": "success",
                    "app": {"id": app_id},
                }
                for index in range(1, 101)
            ]
        }
        exact = {
            "id": 101,
            "name": context,
            "head_sha": head,
            "status": "completed",
            "conclusion": "success",
            "app": {"id": app_id},
        }
        pages = [first_page, {"check_runs": [exact]}]

        matched = validate_exclusivity(
            pages,
            context=context,
            app_id=app_id,
            expected_count=1,
            expected_check_id=101,
            head_sha=head,
        )

        self.assertEqual([exact], matched)
        with self.assertRaisesRegex(ExclusivityError, "expected 0"):
            validate_exclusivity(
                pages,
                context=context,
                app_id=app_id,
                expected_count=0,
                head_sha=head,
            )
        with self.assertRaisesRegex(ExclusivityError, "expected 1"):
            validate_exclusivity(
                [first_page],
                context=context,
                app_id=app_id,
                expected_count=1,
                head_sha=head,
            )
        second_exact = {**exact, "id": 102}
        with self.assertRaisesRegex(ExclusivityError, "found 2"):
            validate_exclusivity(
                [first_page, {"check_runs": [exact, second_exact]}],
                context=context,
                app_id=app_id,
                expected_count=1,
                head_sha=head,
            )
        with self.assertRaisesRegex(ExclusivityError, "exceeds 10"):
            validate_exclusivity(
                [first_page] * 11,
                context=context,
                app_id=app_id,
                expected_count=0,
                head_sha=head,
            )
        with self.assertRaisesRegex(ExclusivityError, "duplicate ids"):
            validate_exclusivity(
                [first_page, {"check_runs": [dict(first_page["check_runs"][0])]}],
                context=context,
                app_id=app_id,
                expected_count=0,
                head_sha=head,
            )
        with self.assertRaisesRegex(ExclusivityError, "exceeds 100"):
            validate_exclusivity(
                [{"check_runs": first_page["check_runs"] + [exact]}],
                context=context,
                app_id=app_id,
                expected_count=0,
                head_sha=head,
            )

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
            subprocess.run(
                ["git", "config", "core.autocrlf", "true"], cwd=root, check=True
            )
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
            (checkout / "release.json").write_text("{}\n", encoding="utf-8")
            receipt = root / "receipt.json"
            receipt.write_text("{}\n", encoding="utf-8")
            attestation = root / "attestation.json"
            attestation.write_text("{}\n", encoding="utf-8")
            digest = lambda path: f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
            request["target"]["releaseContractSha256"] = digest(checkout / "release.json")
            request["target"]["lifecycleReceiptSha256"] = digest(receipt)
            request["target"]["distributionAttestationSha256"] = digest(attestation)
            proof = {
                "schemaVersion": 1,
                "kind": "engineering-process-target-repository-proof",
                "provider": "github",
                "repository": request["target"]["repository"],
                "repositoryId": "123",
                "repositoryUrl": "https://api.github.com/repos/owner/engineering-process",
                "releaseId": "456",
                "releaseUrl": "https://api.github.com/repos/owner/engineering-process/releases/456",
                "tag": request["target"]["tag"],
                "commit": request["target"]["commit"],
                "immutable": True,
                "assets": [
                    {
                        "artifactId": str(700 + index),
                        "name": item["name"],
                        "url": f"https://api.github.com/repos/owner/engineering-process/releases/assets/{700 + index}",
                        "sizeBytes": item["sizeBytes"],
                        "sha256": item["sha256"],
                    }
                    for index, item in enumerate(request["target"]["artifacts"])
                ],
            }
            request["target"]["repositoryProofSha256"] = canonical_json_digest(proof)
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
                    repository_proof_document=proof,
                )
            self.assertEqual(request["target"]["commit"], result["commit"])
            release_check.assert_called_once()
            attestation_check.assert_called_once()
            request["target"]["repository"] = "attacker/engineering-process"
            with self.assertRaisesRegex(ContractError, "registered target"):
                validate_transition_target_provenance(
                    request,
                    target_checkout=checkout,
                    artifact_root=artifacts,
                    release_receipt_path=receipt,
                    artifact_attestation_path=attestation,
                    repository_proof_document=proof,
                )
            request["target"]["repository"] = "owner/engineering-process"
            receipt.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "provenance mismatch"):
                with mock.patch("engineering_process.release.validate_release_checkpoint", return_value=release_result):
                    validate_transition_target_provenance(
                        request,
                        target_checkout=checkout,
                        artifact_root=artifacts,
                        release_receipt_path=receipt,
                        artifact_attestation_path=attestation,
                        repository_proof_document=proof,
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
                    repository_proof_document={},
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

            with mock.patch("engineering_process.lifecycle._resolve_target_repository_proof", return_value={}), mock.patch("engineering_process.lifecycle.validate_transition_target_provenance", return_value={}):
                state, _ = register_authority_transition(root, "adopt-process-0-9-0", request_path, root, root, request_path, request_path, actor_id="worker", context_id="ctx", kind="agent")

            self.assertEqual(3, state["schemaVersion"])
            self.assertIsNone(state["authorityTransition"]["candidateEvidence"])
            self.assertEqual(3, load_state(root, "adopt-process-0-9-0")["schemaVersion"])
            with self.assertRaisesRegex(ContractError, "already registered"):
                register_authority_transition(root, "adopt-process-0-9-0", request_path, root, root, request_path, request_path, actor_id="worker", context_id="ctx", kind="agent")

    def test_cli_registers_transition_with_real_lifecycle_and_n1_probes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = root / "control"
            authority = root / "authority"
            (authority / "engineering_process").mkdir(parents=True)
            (authority / "bin").mkdir()
            if os.name != "nt":
                (authority / "bin" / "python").symlink_to(Path(sys.executable))
            (authority / "engineering_process" / "__init__.py").write_text(
                'VERSION = "0.7.0"\n', encoding="utf-8"
            )
            digest = f"sha256:{'0' * 64}"
            processctl = authority / "bin" / "processctl"
            processctl.write_text(
                "import json, sys\n"
                "if sys.argv[1] == 'digest':\n"
                f" print(json.dumps({{'status':'passed','digest':'{digest}','skills':['run-change']}}))\n"
                "elif sys.argv[1] == 'doctor':\n"
                " print(json.dumps({'status':'passed','processVersion':'0.7.0','project':'sample-project','issues':[]}))\n"
                "else: raise SystemExit(2)\n",
                encoding="utf-8",
            )
            control.mkdir()
            self.initialize(control)
            self.write_contract_and_plan(control)
            source = source_state(control)
            file_digest = lambda path: (
                "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            )
            request = json.loads(
                (Path(__file__).resolve().parent.parent / "examples" / "authority-transition-request.json").read_text(
                    encoding="utf-8"
                )
            )
            request["project"] = "sample-project"
            request["changeId"] = "adopt-process-0-9-0"
            request["source"] = {
                "authority": {"version": "0.7.0", "digest": digest},
                "checkpoint": source["checkpoint"],
                "workspaceFingerprint": source["fingerprint"],
                "processLockSha256": file_digest(control / ".process" / "process.lock"),
                "requirementsLockSha256": file_digest(control / "requirements" / "process.txt"),
            }
            request["candidate"]["baseCheckpoint"] = source["checkpoint"]
            request_path = control / ".process" / "runs" / "_inputs" / "request.json"
            request_path.write_text(json.dumps(request) + "\n", encoding="utf-8")
            stdout = io.StringIO()
            command = [
                "change", "transition", "register",
                "--project-root", str(control),
                "--process-root", str(authority),
                "--actor", "worker", "--context", "ctx",
                "--actor-kind", "agent",
                "--change-id", request["changeId"],
                "--request", str(request_path),
                "--target-checkout", str(control),
                "--artifact-root", str(control),
                "--release-receipt", str(request_path),
                "--artifact-attestation", str(request_path),
                "--json",
            ]

            def installed_authority_commands():
                if os.name == "nt":
                    return mock.patch(
                        "engineering_process.cli._transition_authority_commands",
                        return_value=(Path(sys.executable), processctl),
                    )
                return contextlib.nullcontext()

            wrong_root = root / "wrong-authority"
            wrong_root.mkdir()
            wrong_root_command = command.copy()
            process_root_index = wrong_root_command.index("--process-root") + 1
            wrong_root_command[process_root_index] = str(wrong_root)
            wrong_root_stdout = io.StringIO()
            with contextlib.redirect_stdout(wrong_root_stdout):
                self.assertEqual(2, cli_main(wrong_root_command))
            self.assertIn(
                "installed transition source authority Python is unavailable",
                wrong_root_stdout.getvalue(),
            )

            (authority / "engineering_process" / "__init__.py").write_text(
                'VERSION = "0.8.0-invalid"\n', encoding="utf-8"
            )
            wrong_version_stdout = io.StringIO()
            with (
                installed_authority_commands(),
                contextlib.redirect_stdout(wrong_version_stdout),
            ):
                self.assertEqual(2, cli_main(command))
            self.assertIn(
                "version does not match process.lock",
                wrong_version_stdout.getvalue(),
            )
            (authority / "engineering_process" / "__init__.py").write_text(
                'VERSION = "0.7.0"\n', encoding="utf-8"
            )

            processctl.write_text(
                "import json, sys\n"
                "if sys.argv[1] == 'digest':\n"
                f" print(json.dumps({{'status':'passed','digest':'sha256:{'9' * 64}','skills':['run-change']}}))\n"
                "elif sys.argv[1] == 'doctor':\n"
                " print(json.dumps({'status':'passed','processVersion':'0.7.0','project':'sample-project','issues':[]}))\n",
                encoding="utf-8",
            )
            wrong_digest_stdout = io.StringIO()
            with (
                installed_authority_commands(),
                contextlib.redirect_stdout(wrong_digest_stdout),
            ):
                self.assertEqual(2, cli_main(command))
            self.assertIn(
                "digest does not match process.lock",
                wrong_digest_stdout.getvalue(),
            )
            processctl.write_text(
                "import json, sys\n"
                "if sys.argv[1] == 'digest':\n"
                f" print(json.dumps({{'status':'passed','digest':'{digest}','skills':['run-change']}}))\n"
                "elif sys.argv[1] == 'doctor':\n"
                " print(json.dumps({'status':'passed','processVersion':'0.7.0','project':'sample-project','issues':[]}))\n"
                "else: raise SystemExit(2)\n",
                encoding="utf-8",
            )
            with (
                installed_authority_commands(),
                mock.patch(
                    "engineering_process.cli.lifecycle_environment_issues",
                    return_value=[],
                ),
                mock.patch(
                    "engineering_process.lifecycle._resolve_target_repository_proof",
                    return_value={},
                ),
                mock.patch(
                    "engineering_process.lifecycle.validate_transition_target_provenance",
                    return_value={},
                ),
                contextlib.redirect_stdout(stdout),
            ):
                result = cli_main(command)
            self.assertEqual(0, result, stdout.getvalue())
            self.assertEqual(
                3, load_state(control, request["changeId"])["schemaVersion"]
            )
            self.assertEqual(
                "change transition register",
                json.loads(stdout.getvalue())["command"],
            )

            missing_request = command.copy()
            request_index = missing_request.index("--request")
            del missing_request[request_index : request_index + 2]
            with self.assertRaises(SystemExit):
                cli_main(missing_request)

            ordinary_stdout = io.StringIO()
            with contextlib.redirect_stdout(ordinary_stdout):
                ordinary_result = cli_main(
                    [
                        "change", "status",
                        "--project-root", str(control),
                        "--process-root", str(authority),
                        "--change-id", "ordinary-change",
                        "--json",
                    ]
                )
            self.assertEqual(2, ordinary_result)
            self.assertNotIn(
                "installed transition source authority", ordinary_stdout.getvalue()
            )

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
            with mock.patch("engineering_process.lifecycle._resolve_target_repository_proof", return_value={}), mock.patch("engineering_process.lifecycle.validate_transition_target_provenance", return_value={}):
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
