import hashlib
import json
import platform
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from engineering_process.contracts import (
    CORE_QUALITY_DIMENSIONS,
    Check,
    ContractError,
    Project,
    RemoteVerificationExecution,
    RemoteVerificationRequirement,
    RemoteVerificationSelector,
    canonical_json_digest,
    read_json,
)
from engineering_process.evidence import export_receipt, validate_receipt
from engineering_process.lifecycle import (
    begin_implementation,
    finish_change,
    ingest_remote_verification,
    load_state,
    register_plan,
    request_remote_verification,
    start_change,
    start_review,
    submit_review,
    verify_change,
)
from engineering_process.supplemental import _check_summaries, _impact_summary


class RemoteVerificationTests(unittest.TestCase):
    def initialize_repository(self, root: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "remote-test@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Remote Test"],
            cwd=root,
            check=True,
        )
        (root / ".gitignore").write_text(".process/runs/\n", encoding="utf-8")
        (root / ".process").mkdir()
        (root / ".process" / "process.lock").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "process": {
                        "version": "0.5.1",
                        "digest": f"sha256:{'0' * 64}",
                    },
                    "skills": ["run-change"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "tracked.txt").write_text("before\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", ".gitignore", ".process/process.lock", "tracked.txt"],
            cwd=root,
            check=True,
        )
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)

    def project(self) -> Project:
        def passing(identifier: str) -> Check:
            return Check(
                identifier=identifier,
                run=(sys.executable, "-c", "raise SystemExit(0)"),
                timeout_seconds=10,
                working_directory=".",
            )

        minor = f"{sys.version_info.major}.{sys.version_info.minor}"
        requirement = RemoteVerificationRequirement(
            identifier="supported-python-platforms",
            profiles=("development", "review"),
            execution=RemoteVerificationExecution(
                provider="example-actions",
                repository="example/sample-project",
                workflow="CI",
                workflow_ref=(
                    "example/sample-project/.ci/verify.yml@refs/heads/main"
                ),
            ),
            selectors=(
                RemoteVerificationSelector(
                    identifier=f"linux-python-{minor.replace('.', '-')}",
                    runner_os="Linux",
                    runner_arch=None,
                    implementation=platform.python_implementation(),
                    python_minor=minor,
                ),
            ),
        )
        return Project(
            identifier="sample-project",
            profiles={
                "development": (passing("unit"),),
                "review": (passing("review"),),
            },
            required_profiles=("development", "review"),
            remote_verification={requirement.identifier: requirement},
        )

    def write_contract(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "schemaVersion": 3,
                    "id": "remote-change",
                    "summary": "Require exact remote evidence",
                    "source": "request",
                    "comparisonBase": "HEAD",
                    "specification": {
                        "kind": "change-contract",
                        "reference": "request",
                        "rationale": "The fixture defines the complete behavior.",
                    },
                    "risk": "high",
                    "affectedProjects": ["sample-project"],
                    "acceptanceCriteria": [
                        {"id": "ac-1", "outcome": "Remote evidence passes"}
                    ],
                    "requiredProfiles": ["development", "review"],
                    "requiredEvidence": ["supported-python-platforms"],
                    "quality": {
                        "standard": "production-v1",
                        "assessments": [
                            {
                                "dimension": dimension,
                                "status": "applicable",
                                "rationale": "The fixture exercises this dimension.",
                                "criteria": ["ac-1"],
                            }
                            for dimension in CORE_QUALITY_DIMENSIONS
                        ],
                    },
                    "signOff": {
                        "required": True,
                        "status": "approved",
                        "evidence": "The test owner approved remote verification.",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def write_plan(self, path: Path, digest: str) -> None:
        path.write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "changeId": "remote-change",
                    "contractDigest": digest,
                    "approach": "Use the governed remote evidence owner.",
                    "workItems": [
                        {
                            "id": "work-1",
                            "outcome": "Bind exact remote evidence",
                            "affectedPaths": ["tracked.txt"],
                            "verificationProfiles": ["development", "review"],
                        }
                    ],
                    "acceptancePlan": [
                        {
                            "criterionId": "ac-1",
                            "workItems": ["work-1"],
                            "verificationProfiles": ["development", "review"],
                        }
                    ],
                    "risks": [],
                    "openDecisions": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def prepare_implementing(self, root: Path, inputs: Path):
        contract_path = inputs / "contract.json"
        plan_path = inputs / "plan.json"
        self.write_contract(contract_path)
        state = start_change(
            root,
            self.project(),
            contract_path,
            actor_id="worker",
            context_id="worker-context",
            kind="agent",
        )
        self.write_plan(plan_path, state["contract"]["digest"])
        register_plan(
            root,
            self.project(),
            "remote-change",
            plan_path,
            actor_id="worker",
            context_id="worker-context",
            kind="agent",
        )
        begin_implementation(
            root,
            "remote-change",
            actor_id="worker",
            context_id="worker-context",
            kind="agent",
        )
        (root / "tracked.txt").write_text("after\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "change"], cwd=root, check=True)
        state, request = request_remote_verification(
            root,
            self.project(),
            "remote-change",
            actor_id="worker",
            context_id="worker-context",
            kind="agent",
        )
        reports = {}
        for profile_name in ("development", "review"):
            state, report = verify_change(
                root,
                self.project(),
                "remote-change",
                profile_name,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            reports[profile_name] = report
        self.assertEqual("implementing", state["phase"])
        return state, request, reports

    def write_evidence(
        self,
        inputs: Path,
        request: dict,
        reports: dict[str, dict],
        *,
        diagnostics_failure: bool = False,
    ) -> Path:
        requirement = request["requirements"][0]
        selector = requirement["selectors"][0]
        run_id = "1234"
        run_url = "https://example.invalid/example/sample-project/actions/runs/1234"
        serialized_reports = {}
        entries = []
        for profile_name in requirement["profiles"]:
            report = json.loads(json.dumps(reports[profile_name]))
            if diagnostics_failure:
                report["checks"][0]["diagnostics"] = {
                    "policy": "forbid-warning-error",
                    "status": "failed",
                    "count": 1,
                    "matches": [
                        {
                            "severity": "error",
                            "stream": "stderr",
                            "line": 1,
                            "lineSha256": "1" * 64,
                        }
                    ],
                    "matchesTruncated": False,
                }
            name = f"{profile_name}.json"
            content = (
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8")
            serialized_reports[name] = content
            entries.append(
                {
                    "path": name,
                    "bytes": len(content),
                    "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
                    "schemaVersion": report["schemaVersion"],
                    "profile": report["profile"],
                    "status": report["status"],
                    "checkpoint": report["checkpoint"],
                    "workspaceFingerprint": report["workspaceFingerprint"],
                    "completedWorkspaceFingerprint": report[
                        "completedWorkspaceFingerprint"
                    ],
                    "impact": _impact_summary(report),
                    "checks": _check_summaries(report),
                }
            )
        manifest = {
            "schemaVersion": 2,
            "kind": "engineering-process-supplemental-verification",
            "status": "passed",
            "checkpoint": request["checkpoint"],
            "comparisonBase": request["comparisonBase"],
            "workspaceFingerprint": request["workspaceFingerprint"],
            "startedAt": "2026-01-01T00:00:00Z",
            "completedAt": "2026-01-01T00:01:00Z",
            "producer": {
                "actorId": "example-actions",
                "contextId": "1234:1:verify:Linux",
                "kind": "automation",
            },
            "execution": {
                "provider": requirement["execution"]["provider"],
                "repository": requirement["execution"]["repository"],
                "event": "workflow_dispatch",
                "workflow": requirement["execution"]["workflow"],
                "workflowRef": requirement["execution"]["workflowRef"],
                "workflowSha": requirement["execution"]["workflowSha"],
                "runId": run_id,
                "runAttempt": 1,
                "job": "verify",
                "runUrl": run_url,
                "triggeredBy": "worker",
            },
            "platform": {
                "runnerOs": selector["runnerOs"],
                "runnerArch": "X64",
                "sysPlatform": sys.platform,
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine() or "unknown",
            },
            "runtime": {
                "implementation": selector["implementation"],
                "pythonVersion": platform.python_version(),
                "cacheTag": sys.implementation.cache_tag,
            },
            "reports": entries,
        }
        archive_path = inputs / "remote-evidence.zip"
        with zipfile.ZipFile(
            archive_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
            )
            for name, content in serialized_reports.items():
                archive.writestr(name, content)
        archive_bytes = archive_path.read_bytes()
        archive_digest = f"sha256:{hashlib.sha256(archive_bytes).hexdigest()}"
        evidence_path = inputs / "remote-evidence.json"
        evidence_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "kind": "engineering-process-remote-verification-evidence",
                    "requestSha256": canonical_json_digest(request),
                    "capturedAt": "2026-01-01T00:02:00Z",
                    "artifacts": [
                        {
                            "requirementId": requirement["id"],
                            "selectorId": selector["id"],
                            "archive": {
                                "path": archive_path.name,
                                "bytes": len(archive_bytes),
                                "sha256": archive_digest,
                            },
                            "service": {
                                "artifactId": "5678",
                                "name": "sample-project-ci-evidence-linux",
                                "sizeInBytes": len(archive_bytes),
                                "digest": archive_digest,
                                "runId": run_id,
                                "runAttempt": 1,
                                "runUrl": run_url,
                            },
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return evidence_path

    def review_quality(self):
        return {
            "standard": "production-v1",
            "assessments": [
                {
                    "dimension": dimension,
                    "status": "verified",
                    "criteria": ["ac-1"],
                    "evidence": "The exact local and remote evidence passed.",
                }
                for dimension in CORE_QUALITY_DIMENSIONS
            ],
        }

    def test_remote_evidence_blocks_review_then_completes_and_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            state, request, reports = self.prepare_implementing(root, inputs)
            with self.assertRaisesRegex(
                ContractError, "implementing; expected verified"
            ):
                start_review(
                    root,
                    "remote-change",
                    actor_id="reviewer",
                    context_id="review-context",
                    kind="agent",
                    method="isolated-context",
                    attested_by="test-host",
                    evidence="The test host created a fresh read-only context.",
                )
            evidence_path = self.write_evidence(inputs, request, reports)
            state, _ = ingest_remote_verification(
                root,
                "remote-change",
                evidence_path,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            self.assertEqual("verified", state["phase"])
            state, assignment = start_review(
                root,
                "remote-change",
                actor_id="reviewer",
                context_id="review-context",
                kind="agent",
                method="isolated-context",
                attested_by="test-host",
                evidence="The test host created a fresh read-only context.",
            )
            self.assertIsNotNone(assignment["remoteVerification"]["evidence"])
            review_path = inputs / "review.json"
            review_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 3,
                        "changeId": "remote-change",
                        "cycle": 1,
                        "checkpoint": assignment["checkpoint"],
                        "workspaceFingerprint": assignment[
                            "workspaceFingerprint"
                        ],
                        "comparisonBase": assignment["comparisonBase"],
                        "reviewer": assignment["reviewer"],
                        "independence": assignment["independence"],
                        "quality": self.review_quality(),
                        "verdict": "approved",
                        "findings": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            state = submit_review(root, "remote-change", review_path)
            self.assertEqual("approved", state["phase"])
            state, completion = finish_change(
                root,
                "remote-change",
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            self.assertEqual("completed", state["phase"])
            self.assertEqual(
                state["remoteVerification"]["evidence"],
                completion["remoteVerification"],
            )
            receipt_path = inputs / "receipt.json"
            export_receipt(root, "remote-change", receipt_path)
            validated = validate_receipt(receipt_path)
            self.assertEqual("remote-change", validated["changeId"])

    def test_remote_evidence_rejects_failed_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            _, request, reports = self.prepare_implementing(root, inputs)
            evidence_path = self.write_evidence(
                inputs, request, reports, diagnostics_failure=True
            )
            with self.assertRaisesRegex(
                ContractError, "passing check has contradictory evidence"
            ):
                ingest_remote_verification(
                    root,
                    "remote-change",
                    evidence_path,
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )
            state = load_state(root, "remote-change")
            self.assertEqual("improvement-required", state["phase"])
            self.assertEqual("external-integration", state["improvements"][0]["trigger"])

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlink support")
    def test_remote_evidence_rejects_symlink_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            _, request, reports = self.prepare_implementing(root, inputs)
            evidence_path = self.write_evidence(inputs, request, reports)
            archive = inputs / "remote-evidence.zip"
            real_archive = inputs / "real-remote-evidence.zip"
            archive.rename(real_archive)
            archive.symlink_to(real_archive.name)
            with self.assertRaisesRegex(ContractError, "non-symlink"):
                ingest_remote_verification(
                    root,
                    "remote-change",
                    evidence_path,
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )
