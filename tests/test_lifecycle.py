import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import engineering_process.lifecycle as lifecycle_module
from engineering_process.contracts import (
    CORE_QUALITY_DIMENSIONS,
    Check,
    ContractError,
    ImpactComponent,
    Project,
    ProjectImpact,
)
from engineering_process.evidence import (
    _canonical_digest,
    export_receipt,
    prune_completed_run,
    validate_receipt,
)
from engineering_process.lifecycle import (
    _change_lock,
    begin_implementation,
    finish_change,
    load_state,
    register_plan,
    start_change,
    start_review,
    submit_review,
    verify_change,
)


class LifecycleTests(unittest.TestCase):
    repository_autocrlf: str | None = None

    def initialize_repository(self, root: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        if self.repository_autocrlf is not None:
            subprocess.run(
                ["git", "config", "core.autocrlf", self.repository_autocrlf],
                cwd=root,
                check=True,
            )
        subprocess.run(
            ["git", "config", "user.email", "process-test@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Process Test"],
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
                        "version": "0.1.1",
                        "digest": f"sha256:{'0' * 64}",
                    },
                    "skills": ["run-change"],
                }
            ),
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
        passing = lambda identifier: Check(
            identifier=identifier,
            run=(sys.executable, "-c", "raise SystemExit(0)"),
            timeout_seconds=10,
            working_directory=".",
        )
        return Project(
            identifier="sample-project",
            profiles={
                "development": (passing("unit"),),
                "review": (passing("review"),),
            },
            required_profiles=("development", "review"),
        )

    def write_contract(self, path: Path, *, change_id: str = "change-1") -> None:
        path.write_text(
            json.dumps(
                {
                    "schemaVersion": 3,
                    "id": change_id,
                    "summary": "Change tracked behavior",
                    "source": "request-1",
                    "comparisonBase": "HEAD",
                    "specification": {
                        "kind": "change-contract",
                        "reference": "request-1",
                        "rationale": "The bounded technical behavior is fully specified here.",
                    },
                    "risk": "medium",
                    "affectedProjects": ["sample-project"],
                    "acceptanceCriteria": [
                        {"id": "ac-1", "outcome": "The behavior is implemented"}
                    ],
                    "requiredProfiles": ["development", "review"],
                    "quality": {
                        "standard": "production-v1",
                        "assessments": [
                            {
                                "dimension": dimension,
                                "status": "applicable",
                                "rationale": "The fixture exercises the governed lifecycle.",
                                "criteria": ["ac-1"],
                            }
                            for dimension in CORE_QUALITY_DIMENSIONS
                        ],
                    },
                    "signOff": {
                        "required": False,
                        "status": "not-required",
                        "evidence": None,
                    },
                }
            ),
            encoding="utf-8",
        )

    def write_plan(
        self, path: Path, digest: str, *, change_id: str = "change-1"
    ) -> None:
        path.write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "changeId": change_id,
                    "contractDigest": digest,
                    "approach": "Use the existing owner",
                    "workItems": [
                        {
                            "id": "work-1",
                            "outcome": "Implement and test the behavior",
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
            ),
            encoding="utf-8",
        )

    def review_quality(self, *, failed: tuple[str, ...] = ()):
        return {
            "standard": "production-v1",
            "assessments": [
                {
                    "dimension": dimension,
                    "status": "failed" if dimension in failed else "verified",
                    "criteria": ["ac-1"],
                    "evidence": "The independent fixture review verified this dimension.",
                }
                for dimension in CORE_QUALITY_DIMENSIONS
            ],
        }

    def prepare_verified_change(
        self, root: Path, inputs: Path, *, change_id: str = "change-1"
    ):
        contract_path = inputs / "contract.json"
        plan_path = inputs / "plan.json"
        self.write_contract(contract_path, change_id=change_id)
        state = start_change(
            root,
            self.project(),
            contract_path,
            actor_id="worker",
            context_id="worker-context",
            kind="agent",
        )
        self.write_plan(
            plan_path, state["contract"]["digest"], change_id=change_id
        )
        state = register_plan(
            root,
            self.project(),
            change_id,
            plan_path,
            actor_id="worker",
            context_id="worker-context",
            kind="agent",
        )
        self.assertEqual(state["phase"], "planned")
        begin_implementation(
            root,
            change_id,
            actor_id="worker",
            context_id="worker-context",
            kind="agent",
        )
        state, _ = verify_change(
            root,
            self.project(),
            change_id,
            "development",
            actor_id="worker",
            context_id="worker-context",
            kind="agent",
        )
        self.assertEqual(state["phase"], "implementing")
        state, _ = verify_change(
            root,
            self.project(),
            change_id,
            "review",
            actor_id="worker",
            context_id="worker-context",
            kind="agent",
        )
        self.assertEqual(state["phase"], "verified")
        return state

    def test_verification_uses_registered_contract_comparison_base(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            contract_path = inputs / "contract.json"
            plan_path = inputs / "plan.json"
            self.write_contract(contract_path)
            unscoped = self.project()
            project = Project(
                identifier=unscoped.identifier,
                profiles=unscoped.profiles,
                required_profiles=unscoped.required_profiles,
                impact=ProjectImpact(
                    base_refs=("missing-default",),
                    unmatched_paths="all-scoped-checks",
                    components={
                        "source": ImpactComponent(
                            identifier="source",
                            paths=("tracked.txt",),
                            affects=(),
                        )
                    },
                ),
            )
            state = start_change(
                root,
                project,
                contract_path,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            self.write_plan(plan_path, state["contract"]["digest"])
            register_plan(
                root,
                project,
                "change-1",
                plan_path,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            begin_implementation(
                root,
                "change-1",
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )

            _, report = verify_change(
                root,
                project,
                "change-1",
                "development",
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )

            self.assertEqual(report["impact"]["baseRef"], "HEAD")
            self.assertEqual(report["impact"]["changedPaths"], [])

    def test_full_lifecycle_requires_independent_review(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            state = self.prepare_verified_change(root, inputs)

            with self.assertRaisesRegex(ContractError, "independent review"):
                start_review(
                    root,
                    "change-1",
                    actor_id="worker",
                    context_id="review-context",
                    kind="agent",
                    method="isolated-context",
                    attested_by="test-host",
                    evidence="A separate reviewer was requested",
                )

            with self.assertRaisesRegex(ContractError, "independent review"):
                start_review(
                    root,
                    "change-1",
                    actor_id="different-name",
                    context_id="worker-context",
                    kind="agent",
                    method="isolated-context",
                    attested_by="test-host",
                    evidence="The context was incorrectly reused",
                )

            state, assignment = start_review(
                root,
                "change-1",
                actor_id="reviewer",
                context_id="review-context",
                kind="agent",
                method="isolated-context",
                attested_by="test-host",
                evidence="The test host created a context not used by implementation",
            )
            self.assertEqual(state["phase"], "review-pending")
            report_path = inputs / "review.json"
            report_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 3,
                        "changeId": "change-1",
                        "cycle": 1,
                        "checkpoint": assignment["checkpoint"],
                        "workspaceFingerprint": assignment["workspaceFingerprint"],
                        "comparisonBase": assignment["comparisonBase"],
                        "reviewer": assignment["reviewer"],
                        "independence": assignment["independence"],
                        "quality": self.review_quality(),
                        "verdict": "approved",
                        "findings": [],
                    }
                ),
                encoding="utf-8",
            )
            state = submit_review(root, "change-1", report_path)
            self.assertEqual(state["phase"], "approved")
            state, completion = finish_change(
                root,
                "change-1",
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            self.assertEqual(state["phase"], "completed")
            self.assertEqual(completion["checkpoint"], assignment["checkpoint"])
            receipt_path = inputs / "receipt.json"
            exported = export_receipt(root, "change-1", receipt_path)
            self.assertEqual("change-1", exported["changeId"])
            self.assertEqual(exported, validate_receipt(receipt_path))
            tampered_path = inputs / "tampered-receipt.json"
            tampered = json.loads(receipt_path.read_text(encoding="utf-8"))
            contract_entry = tampered["artifacts"]["contract"]
            contract_entry["sourceText"] = contract_entry["sourceText"].replace(
                "Change tracked behavior", "tampered"
            )
            tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "canonicalDigest|sourceDigest"):
                validate_receipt(tampered_path)
            incomplete_path = inputs / "incomplete-verification-receipt.json"
            incomplete = json.loads(receipt_path.read_text(encoding="utf-8"))
            entry = incomplete["artifacts"]["verification"][0]
            report = json.loads(entry["sourceText"])
            incomplete_report = {
                name: report[name]
                for name in ("profile", "status", "checkpoint", "workspaceFingerprint")
            }
            entry["sourceText"] = json.dumps(incomplete_report)
            entry["sourceDigest"] = (
                "sha256:"
                + hashlib.sha256(entry["sourceText"].encode("utf-8")).hexdigest()
            )
            entry["canonicalDigest"] = _canonical_digest(incomplete_report)
            incomplete_path.write_text(json.dumps(incomplete), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "schemaVersion"):
                validate_receipt(incomplete_path)
            semantic_path = inputs / "semantic-tamper-receipt.json"
            semantic = json.loads(receipt_path.read_text(encoding="utf-8"))
            entry = semantic["artifacts"]["verification"][0]
            report = json.loads(entry["sourceText"])
            report["checks"][0]["command"].append("tampered")
            entry["sourceText"] = json.dumps(report)
            entry["sourceDigest"] = (
                "sha256:"
                + hashlib.sha256(entry["sourceText"].encode("utf-8")).hexdigest()
            )
            entry["canonicalDigest"] = _canonical_digest(report)
            semantic_path.write_text(json.dumps(semantic), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "does not match command"):
                validate_receipt(semantic_path)
            preview = prune_completed_run(
                root, "change-1", receipt_path, apply=False
            )
            self.assertFalse(preview["applied"])
            receipt_document = json.loads(receipt_path.read_text(encoding="utf-8"))
            contract = receipt_document["artifacts"]["contract"]
            contract_path = receipt_document["state"]["document"]["contract"]["path"]
            quarantine_path: Path | None = None

            def fail_after_partial_delete(path: Path) -> None:
                nonlocal quarantine_path
                quarantine_path = Path(path)
                (quarantine_path / Path(contract_path).name).unlink()
                raise OSError("simulated partial deletion")

            with (
                mock.patch(
                    "engineering_process.evidence.shutil.rmtree",
                    side_effect=fail_after_partial_delete,
                ),
                self.assertRaisesRegex(
                    ContractError, "no restored run is claimed.*validated receipt"
                ),
            ):
                prune_completed_run(root, "change-1", receipt_path, apply=True)
            run_root = root / ".process" / "runs" / "change-1"
            self.assertFalse(run_root.exists())
            self.assertIsNotNone(quarantine_path)
            assert quarantine_path is not None
            self.assertTrue(quarantine_path.exists())
            self.assertFalse((quarantine_path / Path(contract_path).name).exists())
            self.assertEqual(exported, validate_receipt(receipt_path))

            recovered_contract = quarantine_path / Path(contract_path).name
            recovered_contract.parent.mkdir(parents=True, exist_ok=True)
            recovered_contract.write_text(contract["sourceText"], encoding="utf-8")
            quarantine_path.replace(run_root)
            applied = prune_completed_run(root, "change-1", receipt_path, apply=True)
            self.assertTrue(applied["applied"])
            self.assertFalse((root / ".process" / "runs" / "change-1").exists())

    def test_project_quality_extensions_are_additive_lifecycle_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            contract_path = inputs / "contract.json"
            self.write_contract(contract_path)
            project = self.project()
            project = Project(
                identifier=project.identifier,
                profiles=project.profiles,
                required_profiles=project.required_profiles,
                quality_extensions=("project-accessibility",),
            )

            with self.assertRaisesRegex(
                ContractError, "omits project quality dimensions: project-accessibility"
            ):
                start_change(
                    root,
                    project,
                    contract_path,
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )

    def test_rejected_verification_records_specific_eligibility_reasons(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
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
                "change-1",
                plan_path,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            begin_implementation(
                root,
                "change-1",
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            real_run_profile = lifecycle_module.run_profile

            def dirty_report(*args, **kwargs):
                report = real_run_profile(*args, **kwargs)
                report["workingTreeDirty"] = True
                return report

            with (
                mock.patch.object(
                    lifecycle_module,
                    "run_profile",
                    side_effect=dirty_report,
                ),
                self.assertRaisesRegex(ContractError, "working-tree-dirty"),
            ):
                verify_change(
                    root,
                    self.project(),
                    "change-1",
                    "development",
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )

            state = load_state(root, "change-1")
            rejected = [
                event
                for event in state["history"]
                if event["event"] == "verification-rejected"
            ]
            self.assertEqual(
                ["working-tree-dirty"], rejected[-1]["eligibilityIssues"]
            )

    def test_verification_eligibility_reason_mapping_is_complete(self):
        passing = {
            "status": "passed",
            "checkpoint": "a" * 40,
            "workingTreeDirty": False,
            "workspaceFingerprint": f"sha256:{'1' * 64}",
            "sourceChangedDuringVerification": False,
            "completedWorkspaceFingerprint": f"sha256:{'1' * 64}",
        }
        cases = {
            "profile": (
                {"status": "failed"},
                ["profile-status-not-passed"],
            ),
            "checkpoint": (
                {"checkpoint": None},
                ["checkpoint-missing"],
            ),
            "dirty": (
                {"workingTreeDirty": True},
                ["working-tree-dirty"],
            ),
            "missing-fingerprint": (
                {
                    "workspaceFingerprint": None,
                    "completedWorkspaceFingerprint": None,
                },
                ["workspace-fingerprint-missing"],
            ),
            "source-changed": (
                {"sourceChangedDuringVerification": True},
                ["source-changed-during-verification"],
            ),
            "fingerprint-changed": (
                {"completedWorkspaceFingerprint": f"sha256:{'2' * 64}"},
                ["workspace-fingerprint-changed-during-verification"],
            ),
        }
        for name, (updates, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(
                    expected,
                    lifecycle_module._verification_eligibility_issues(
                        {**passing, **updates}
                    ),
                )

    def test_source_state_rejection_summary_is_attributable_and_bounded(self):
        digest_one = f"sha256:{'1' * 64}"
        digest_two = f"sha256:{'2' * 64}"
        diagnostics = {
            "issues": [
                {
                    "operation": "status",
                    "failureKind": "execution-error",
                    "exitCode": None,
                    "stderr": "",
                    "stderrSha256": hashlib.sha256(b"").hexdigest(),
                    "error": "x" * 700,
                    "errorSha256": hashlib.sha256(b"x" * 700).hexdigest(),
                }
            ],
            "ignoredBytecodeSha256": digest_one,
            "trackedIndexSha256": digest_one,
            "statusSha256": digest_one,
            "diffSha256": digest_one,
            "untrackedSha256": digest_one,
            "untrackedPathCount": 0,
            "untrackedBytes": 0,
        }
        completed = {**diagnostics, "issues": [], "statusSha256": digest_two}

        summary = lifecycle_module._source_state_diagnostic_summary(
            {
                "sourceStateDiagnostics": diagnostics,
                "completedSourceStateDiagnostics": completed,
            }
        )

        self.assertIsNotNone(summary)
        parsed = json.loads(summary or "{}")
        issue = parsed["start"]["issues"][0]
        self.assertEqual("status", issue["operation"])
        self.assertEqual("execution-error", issue["failureKind"])
        self.assertEqual(512, len(issue["detail"]))
        self.assertEqual(
            digest_two,
            parsed["changedComponents"]["statusSha256"]["completion"],
        )

    @unittest.skipUnless(
        sys.platform == "win32", "Windows lifecycle repetition evidence"
    )
    def test_requested_changes_transition_repeats_on_fresh_windows_repositories(self):
        try:
            for autocrlf in ("false", "input", "true"):
                self.repository_autocrlf = autocrlf
                for iteration in range(2):
                    with self.subTest(autocrlf=autocrlf, iteration=iteration):
                        self.test_requested_changes_start_a_new_cycle_and_invalidate_evidence()
        finally:
            self.repository_autocrlf = None

    def test_requested_changes_start_a_new_cycle_and_invalidate_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            self.prepare_verified_change(root, inputs)
            _, assignment = start_review(
                root,
                "change-1",
                actor_id="reviewer",
                context_id="review-context",
                kind="agent",
                method="isolated-context",
                attested_by="test-host",
                evidence="The test host created a context not used by implementation",
            )
            report_path = inputs / "review.json"
            report_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 3,
                        "changeId": "change-1",
                        "cycle": 1,
                        "checkpoint": assignment["checkpoint"],
                        "workspaceFingerprint": assignment["workspaceFingerprint"],
                        "comparisonBase": assignment["comparisonBase"],
                        "reviewer": assignment["reviewer"],
                        "independence": assignment["independence"],
                        "quality": self.review_quality(failed=("correctness",)),
                        "verdict": "changes-requested",
                        "findings": [
                            {
                                "id": "finding-1",
                                "severity": "high",
                                "path": "tracked.txt",
                                "line": 1,
                                "summary": "Behavior is incomplete",
                                "evidence": "The required value is absent",
                                "status": "open",
                                "resolutionEvidence": None,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = submit_review(root, "change-1", report_path)
            self.assertEqual(state["phase"], "changes-requested")
            state = begin_implementation(
                root,
                "change-1",
                actor_id="implementer",
                context_id="fix-context",
                kind="agent",
            )
            self.assertEqual(state["cycle"], 2)
            self.assertEqual(state["verification"], [])
            self.assertIsNone(state["review"])
            self.assertEqual(["finding-1"], [item["id"] for item in state["pendingFindings"]])
            self.assertTrue(
                any(event.get("report") for event in state["history"])
            )

            for profile in ("development", "review"):
                state, _ = verify_change(
                    root,
                    self.project(),
                    "change-1",
                    profile,
                    actor_id="implementer",
                    context_id="fix-context",
                    kind="agent",
                )
            with self.assertRaisesRegex(ContractError, "fresh context id"):
                start_review(
                    root,
                    "change-1",
                    actor_id="reviewer",
                    context_id="review-context",
                    kind="agent",
                    method="isolated-context",
                    attested_by="test-host",
                    evidence=(
                        "The context id was renamed without creating a fresh "
                        "isolated context"
                    ),
                )
            _, next_assignment = start_review(
                root,
                "change-1",
                actor_id="reviewer",
                context_id="second-review-context",
                kind="agent",
                method="isolated-context",
                attested_by="test-host",
                evidence="The test host created a second isolated review context",
            )
            self.assertEqual(
                ["finding-1"],
                [item["id"] for item in next_assignment["pendingFindings"]],
            )
            deferred = dict(next_assignment["pendingFindings"][0])
            deferred["status"] = "deferred"
            deferred["resolutionEvidence"] = "A later change was suggested"
            report_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 3,
                        "changeId": "change-1",
                        "cycle": 2,
                        "checkpoint": next_assignment["checkpoint"],
                        "workspaceFingerprint": next_assignment["workspaceFingerprint"],
                        "comparisonBase": next_assignment["comparisonBase"],
                        "reviewer": next_assignment["reviewer"],
                        "independence": next_assignment["independence"],
                        "quality": self.review_quality(),
                        "verdict": "approved",
                        "findings": [deferred],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ContractError, "open or deferred"):
                submit_review(root, "change-1", report_path)

            report_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 3,
                        "changeId": "change-1",
                        "cycle": 2,
                        "checkpoint": next_assignment["checkpoint"],
                        "workspaceFingerprint": next_assignment["workspaceFingerprint"],
                        "comparisonBase": next_assignment["comparisonBase"],
                        "reviewer": next_assignment["reviewer"],
                        "independence": next_assignment["independence"],
                        "quality": self.review_quality(),
                        "verdict": "approved",
                        "findings": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ContractError, "carry forward pending finding"):
                submit_review(root, "change-1", report_path)

            resolved = dict(next_assignment["pendingFindings"][0])
            resolved["status"] = "resolved"
            resolved["resolutionEvidence"] = "The second review verified the correction"
            report_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 3,
                        "changeId": "change-1",
                        "cycle": 2,
                        "checkpoint": next_assignment["checkpoint"],
                        "workspaceFingerprint": next_assignment["workspaceFingerprint"],
                        "comparisonBase": next_assignment["comparisonBase"],
                        "reviewer": next_assignment["reviewer"],
                        "independence": next_assignment["independence"],
                        "quality": self.review_quality(),
                        "verdict": "approved",
                        "findings": [resolved],
                    }
                ),
                encoding="utf-8",
            )
            state = submit_review(root, "change-1", report_path)
            self.assertEqual("approved", state["phase"])
            self.assertEqual([], state["pendingFindings"])
            state, _ = finish_change(
                root,
                "change-1",
                actor_id="implementer",
                context_id="fix-context",
                kind="agent",
            )
            self.assertEqual("completed", state["phase"])

    def test_source_change_invalidates_review_start(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            self.prepare_verified_change(root, inputs)
            (root / "tracked.txt").write_text("changed\n", encoding="utf-8")

            with self.assertRaisesRegex(ContractError, "stale"):
                start_review(
                    root,
                    "change-1",
                    actor_id="reviewer",
                    context_id="review-context",
                    kind="agent",
                    method="isolated-context",
                    attested_by="test-host",
                    evidence="The test host created an isolated context",
                )

    def test_review_context_cannot_be_reused_across_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            first_inputs = base / "first-inputs"
            second_inputs = base / "second-inputs"
            root.mkdir()
            first_inputs.mkdir()
            second_inputs.mkdir()
            self.initialize_repository(root)
            self.prepare_verified_change(
                root, first_inputs, change_id="change-1"
            )
            start_review(
                root,
                "change-1",
                actor_id="reviewer-role",
                context_id="shared-review-context",
                kind="agent",
                method="isolated-context",
                attested_by="test-host",
                evidence="The test host created one isolated review context",
            )
            self.prepare_verified_change(
                root, second_inputs, change_id="change-2"
            )

            with self.assertRaisesRegex(ContractError, "any review assignment"):
                start_review(
                    root,
                    "change-2",
                    actor_id="reviewer-role",
                    context_id="shared-review-context",
                    kind="agent",
                    method="isolated-context",
                    attested_by="test-host",
                    evidence=(
                        "The same retained context was presented as a new review"
                    ),
                )

    def test_schema_one_state_reconstructs_findings_after_review_was_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            self.prepare_verified_change(root, inputs)
            _, assignment = start_review(
                root,
                "change-1",
                actor_id="reviewer",
                context_id="review-context",
                kind="agent",
                method="isolated-context",
                attested_by="test-host",
                evidence="The test host created an isolated review context",
            )
            report_path = inputs / "review.json"
            report_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 3,
                        "changeId": "change-1",
                        "cycle": 1,
                        "checkpoint": assignment["checkpoint"],
                        "workspaceFingerprint": assignment["workspaceFingerprint"],
                        "comparisonBase": assignment["comparisonBase"],
                        "reviewer": assignment["reviewer"],
                        "independence": assignment["independence"],
                        "quality": self.review_quality(failed=("correctness",)),
                        "verdict": "changes-requested",
                        "findings": [
                            {
                                "id": "legacy-finding",
                                "severity": "high",
                                "path": "tracked.txt",
                                "line": 1,
                                "summary": "Legacy state must preserve this finding",
                                "evidence": "The reviewed behavior remains incomplete",
                                "status": "open",
                                "resolutionEvidence": None,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            submit_review(root, "change-1", report_path)
            begin_implementation(
                root,
                "change-1",
                actor_id="implementer",
                context_id="fix-context",
                kind="agent",
            )
            state_path = root / ".process" / "runs" / "change-1" / "state.json"
            legacy = json.loads(state_path.read_text(encoding="utf-8"))
            legacy["schemaVersion"] = 1
            del legacy["pendingFindings"]
            legacy["phase"] = "approved"
            state_path.write_text(json.dumps(legacy), encoding="utf-8")

            migrated = load_state(root, "change-1")

            self.assertEqual(2, migrated["schemaVersion"])
            self.assertEqual("changes-requested", migrated["phase"])
            self.assertEqual(
                ["legacy-finding"],
                [finding["id"] for finding in migrated["pendingFindings"]],
            )

    @unittest.skipIf(sys.platform == "win32", "same-process Windows lock semantics differ")
    def test_concurrent_mutation_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            contract_path = inputs / "contract.json"
            plan_path = inputs / "plan.json"
            self.write_contract(contract_path)
            state = start_change(
                root,
                self.project(),
                contract_path,
                actor_id="lead",
                context_id="lead-context",
                kind="agent",
            )
            self.write_plan(plan_path, state["contract"]["digest"])
            register_plan(
                root,
                self.project(),
                "change-1",
                plan_path,
                actor_id="planner",
                context_id="plan-context",
                kind="agent",
            )

            with _change_lock(root, "change-1"):
                with self.assertRaisesRegex(ContractError, "another process"):
                    begin_implementation(
                        root,
                        "change-1",
                        actor_id="implementer",
                        context_id="implementation-context",
                        kind="agent",
                    )
