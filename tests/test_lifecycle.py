import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engineering_process.contracts import Check, ContractError, Project
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
    def initialize_repository(self, root: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
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
        (root / "tracked.txt").write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore", "tracked.txt"], cwd=root, check=True)
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

    def write_contract(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "id": "change-1",
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
                    "signOff": {
                        "required": False,
                        "status": "not-required",
                        "evidence": None,
                    },
                }
            ),
            encoding="utf-8",
        )

    def write_plan(self, path: Path, digest: str) -> None:
        path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "changeId": "change-1",
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

    def prepare_verified_change(self, root: Path, inputs: Path):
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
        state = register_plan(
            root,
            self.project(),
            "change-1",
            plan_path,
            actor_id="worker",
            context_id="worker-context",
            kind="agent",
        )
        self.assertEqual(state["phase"], "planned")
        begin_implementation(
            root,
            "change-1",
            actor_id="worker",
            context_id="worker-context",
            kind="agent",
        )
        state, _ = verify_change(
            root,
            self.project(),
            "change-1",
            "development",
            actor_id="worker",
            context_id="worker-context",
            kind="agent",
        )
        self.assertEqual(state["phase"], "implementing")
        state, _ = verify_change(
            root,
            self.project(),
            "change-1",
            "review",
            actor_id="worker",
            context_id="worker-context",
            kind="agent",
        )
        self.assertEqual(state["phase"], "verified")
        return state

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
                        "schemaVersion": 2,
                        "changeId": "change-1",
                        "cycle": 1,
                        "checkpoint": assignment["checkpoint"],
                        "workspaceFingerprint": assignment["workspaceFingerprint"],
                        "comparisonBase": assignment["comparisonBase"],
                        "reviewer": assignment["reviewer"],
                        "independence": assignment["independence"],
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
                        "schemaVersion": 2,
                        "changeId": "change-1",
                        "cycle": 1,
                        "checkpoint": assignment["checkpoint"],
                        "workspaceFingerprint": assignment["workspaceFingerprint"],
                        "comparisonBase": assignment["comparisonBase"],
                        "reviewer": assignment["reviewer"],
                        "independence": assignment["independence"],
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
                        "schemaVersion": 2,
                        "changeId": "change-1",
                        "cycle": 2,
                        "checkpoint": next_assignment["checkpoint"],
                        "workspaceFingerprint": next_assignment["workspaceFingerprint"],
                        "comparisonBase": next_assignment["comparisonBase"],
                        "reviewer": next_assignment["reviewer"],
                        "independence": next_assignment["independence"],
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
                        "schemaVersion": 2,
                        "changeId": "change-1",
                        "cycle": 2,
                        "checkpoint": next_assignment["checkpoint"],
                        "workspaceFingerprint": next_assignment["workspaceFingerprint"],
                        "comparisonBase": next_assignment["comparisonBase"],
                        "reviewer": next_assignment["reviewer"],
                        "independence": next_assignment["independence"],
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
                        "schemaVersion": 2,
                        "changeId": "change-1",
                        "cycle": 2,
                        "checkpoint": next_assignment["checkpoint"],
                        "workspaceFingerprint": next_assignment["workspaceFingerprint"],
                        "comparisonBase": next_assignment["comparisonBase"],
                        "reviewer": next_assignment["reviewer"],
                        "independence": next_assignment["independence"],
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
                        "schemaVersion": 2,
                        "changeId": "change-1",
                        "cycle": 1,
                        "checkpoint": assignment["checkpoint"],
                        "workspaceFingerprint": assignment["workspaceFingerprint"],
                        "comparisonBase": assignment["comparisonBase"],
                        "reviewer": assignment["reviewer"],
                        "independence": assignment["independence"],
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
            state_path.write_text(json.dumps(legacy), encoding="utf-8")

            migrated = load_state(root, "change-1")

            self.assertEqual(2, migrated["schemaVersion"])
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
