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
    MATERIAL_DECISION_CATEGORIES,
    Check,
    ContractError,
    ImpactComponent,
    Project,
    ProjectImpact,
    canonical_json_digest,
)
from engineering_process.evidence import (
    _canonical_digest,
    export_bootstrap_authorization,
    export_receipt,
    prune_completed_run,
    validate_receipt,
    validate_bootstrap_authorization,
)
from engineering_process.improvement import (
    attach_improvement_chain,
    create_improvement_disposition,
    export_improvement_signal,
    ingest_improvement_signal,
    validate_improvement_chain,
)
from engineering_process.recommendation import (
    create_recommendation_resolution,
    start_recommendation_review,
)
from engineering_process.lifecycle import (
    _change_lock,
    begin_implementation,
    classify_improvement_case,
    finish_change,
    load_state,
    lifecycle_status,
    register_plan,
    resolve_plan_decision,
    start_plan_decision_review,
    start_change,
    start_review,
    submit_review,
    submit_plan_decision_review,
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

    def test_review_loop_counter_counts_distinct_cycles_not_finding_labels(self):
        state = {
            "cycle": 1,
            "reviewLoop": {
                "schemaVersion": 1,
                "threshold": 3,
                "window": {
                    "number": 1,
                    "startedAtCycle": 1,
                    "reviewCycles": [],
                },
                "escalations": [],
            },
        }
        self.assertIsNone(
            lifecycle_module._record_review_loop_result(
                state,
                finding_ids=["finding-a", "finding-b"],
                contract_gap_ids=[],
            )
        )
        self.assertIsNone(
            lifecycle_module._record_review_loop_result(
                state,
                finding_ids=["renamed-a", "split-a", "split-b"],
                contract_gap_ids=[],
            )
        )
        self.assertEqual([1], state["reviewLoop"]["window"]["reviewCycles"])
        state["cycle"] = 2
        lifecycle_module._record_review_loop_result(
            state,
            finding_ids=["renamed-c"],
            contract_gap_ids=[],
        )
        state["cycle"] = 3
        escalation = lifecycle_module._record_review_loop_result(
            state,
            finding_ids=["entirely-new-label"],
            contract_gap_ids=[],
        )
        self.assertIsNotNone(escalation)
        self.assertEqual([1, 2, 3], escalation["reviewCycles"])

    def test_cycle_limit_terminal_supersession_closes_current_window(self):
        artifact = {
            "path": ".process/runs/change-1/decision.json",
            "digest": f"sha256:{'1' * 64}",
        }
        state = {
            "reviewLoop": {
                "schemaVersion": 1,
                "threshold": 3,
                "window": {
                    "number": 1,
                    "startedAtCycle": 1,
                    "reviewCycles": [1, 2, 3],
                },
                "escalations": [
                    {
                        "id": "review-loop-0001",
                        "kind": "cycle-limit",
                        "status": "superseded",
                        "triggerCycle": 3,
                        "reviewCycles": [1, 2, 3],
                        "findingIds": ["finding-3"],
                        "decision": {
                            "planDecisionAssignment": artifact,
                            "planDecisionReview": artifact,
                            "recommendation": artifact,
                            "recommendationAssignment": artifact,
                            "recommendationReview": artifact,
                            "resolution": artifact,
                            "selectedOptionId": "stop-change",
                            "lifecycleEffect": "supersede-change",
                        },
                    }
                ],
            }
        }
        lifecycle_module._close_review_loop_window(
            state,
            lifecycle_effect="supersede-change",
            assessment_cycle=4,
        )
        lifecycle_module.validate_review_loop(state["reviewLoop"])
        self.assertEqual(1, state["reviewLoop"]["window"]["number"])
        self.assertEqual(4, state["reviewLoop"]["window"]["startedAtCycle"])
        self.assertEqual([], state["reviewLoop"]["window"]["reviewCycles"])

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

    def plan_decision_project(self) -> Project:
        base = self.project()
        return Project(
            identifier=base.identifier,
            profiles=base.profiles,
            required_profiles=base.required_profiles,
            plan_decision_mode="provenance-gated-authored-review",
            material_decision_categories=MATERIAL_DECISION_CATEGORIES,
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

    def write_finite_contract(
        self, path: Path, *, change_id: str = "change-1"
    ) -> None:
        self.write_contract(path, change_id=change_id)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["schemaVersion"] = 4
        document["reviewBoundary"] = {
            "mode": "finite-fault-model",
            "trustBoundaries": [
                {
                    "id": "tracked-boundary",
                    "owner": "sample-project",
                    "statement": "The project owns tracked behavior.",
                    "criterionIds": ["ac-1"],
                }
            ],
            "faultRows": [
                {
                    "id": "tracked-failure",
                    "trustBoundaryId": "tracked-boundary",
                    "criterionIds": ["ac-1"],
                    "trigger": "The tracked behavior fails.",
                    "injectionBoundary": "The tracked behavior call boundary.",
                    "expectedOutcome": "The failure is bounded and visible.",
                    "proof": {
                        "profiles": ["development", "review"],
                        "evidenceRequirementIds": [],
                        "assertion": "Focused valid and failure regressions pass.",
                    },
                    "stopCondition": "The exact valid and failure rows pass.",
                }
            ],
            "outOfBoundary": "owner-decision-and-supersede",
        }
        path.write_text(json.dumps(document) + "\n", encoding="utf-8")

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

    def write_authored_plan(
        self, path: Path, digest: str, *, change_id: str = "change-1"
    ) -> None:
        self.write_plan(path, digest, change_id=change_id)
        plan = json.loads(path.read_text(encoding="utf-8"))
        plan["schemaVersion"] = 3
        plan["provenance"] = {
            "kind": "authored",
            "author": {
                "actorId": "worker",
                "contextId": "worker-context",
                "kind": "agent",
            },
            "authority": {
                "version": "0.1.1",
                "digest": f"sha256:{'0' * 64}",
            },
        }
        path.write_text(json.dumps(plan) + "\n", encoding="utf-8")

    def write_plan_decision_review(
        self,
        path: Path,
        assignment: dict[str, object],
        *,
        decision_category: str | None = None,
    ) -> None:
        path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "kind": "engineering-process-plan-decision-review",
                    "changeId": assignment["changeId"],
                    "cycle": assignment["cycle"],
                    "contractSha256": assignment["contractSha256"],
                    "planSha256": assignment["planSha256"],
                    "assignmentSha256": canonical_json_digest(assignment),
                    "reviewer": assignment["reviewer"],
                    "categoryAssessments": [
                        {
                            "category": category,
                            "status": (
                                "decision-required"
                                if category == decision_category
                                else "clear"
                            ),
                            "evidence": f"The reviewer assessed {category} against the exact plan.",
                        }
                        for category in MATERIAL_DECISION_CATEGORIES
                    ],
                    "verdict": (
                        "decision-required" if decision_category else "clear"
                    ),
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def resolve_current_plan_decision_with_effect(
        self,
        root: Path,
        inputs: Path,
        project: Project,
        assessment: dict[str, object],
        *,
        lifecycle_effect: str,
        prefix: str,
    ) -> dict[str, object]:
        recommendation = json.loads(
            (
                Path(__file__).resolve().parent.parent
                / "examples"
                / "recommendation.json"
            ).read_text(encoding="utf-8")
        )
        assessment_digest = canonical_json_digest(assessment)
        recommendation["decisionId"] = "change-1"
        recommendation["coordinator"] = {
            "actorId": "worker",
            "contextId": "worker-context",
            "kind": "agent",
        }
        recommendation["invariants"].insert(
            1,
            {
                "id": "plan-decision-assessment",
                "statement": "Bind the exact review-loop plan assessment.",
                "source": f"plan-decision-review:{assessment_digest}",
                "evidenceSha256": assessment_digest,
            },
        )
        for option in recommendation["options"]:
            option["invariantAssessments"].insert(
                1,
                {
                    "invariantId": "plan-decision-assessment",
                    "status": "satisfied",
                    "evidenceSha256": assessment_digest,
                },
            )
            if option["id"] == "verify-before-completion":
                option["lifecycleEffect"] = lifecycle_effect
        recommendation_path = inputs / f"{prefix}-recommendation.json"
        recommendation_path.write_text(
            json.dumps(recommendation) + "\n", encoding="utf-8"
        )
        assignment_result = start_recommendation_review(
            root,
            recommendation_path,
            actor_id=f"{prefix}-recommendation-reviewer",
            context_id=f"fresh-{prefix}-recommendation-context",
            kind="agent",
            method="isolated-context",
            attested_by="test-host",
            evidence="The host created a fresh recommendation review context.",
        )
        assignment_path = root / assignment_result["assignment"]
        assignment = json.loads(assignment_path.read_text(encoding="utf-8"))
        recommendation_review = json.loads(
            (
                Path(__file__).resolve().parent.parent
                / "examples"
                / "recommendation-review.json"
            ).read_text(encoding="utf-8")
        )
        recommendation_review["decisionId"] = "change-1"
        recommendation_review["recommendationSha256"] = canonical_json_digest(
            recommendation
        )
        recommendation_review["assignmentSha256"] = canonical_json_digest(
            assignment
        )
        recommendation_review["reviewer"] = assignment["reviewer"]
        recommendation_review["invariantAssessments"].insert(
            1,
            {
                "invariantId": "plan-decision-assessment",
                "status": "verified",
                "evidence": "The exact plan assessment is bound.",
            },
        )
        review_path = inputs / f"{prefix}-recommendation-review.json"
        review_path.write_text(
            json.dumps(recommendation_review) + "\n", encoding="utf-8"
        )
        resolution_path = inputs / f"{prefix}-resolution.json"
        create_recommendation_resolution(
            root,
            recommendation_path,
            assignment_path,
            review_path,
            selected_option_id="verify-before-completion",
            owner_id="project-owner",
            owner_evidence_sha256=f"sha256:{'9' * 64}",
            selection_rationale_sha256=f"sha256:{'a' * 64}",
            output=resolution_path,
        )
        return resolve_plan_decision(
            root,
            project,
            "change-1",
            recommendation_path=recommendation_path,
            assignment_path=assignment_path,
            review_path=review_path,
            resolution_path=resolution_path,
            actor_id="worker",
            context_id="worker-context",
            kind="agent",
        )

    def prepare_authored_plan_decision(
        self, root: Path, inputs: Path
    ) -> tuple[Project, dict[str, object]]:
        project = self.plan_decision_project()
        contract_path = inputs / "contract.json"
        plan_path = inputs / "plan.json"
        self.write_contract(contract_path)
        state = start_change(
            root,
            project,
            contract_path,
            actor_id="worker",
            context_id="worker-context",
            kind="agent",
        )
        self.write_authored_plan(plan_path, state["contract"]["digest"])
        register_plan(
            root,
            project,
            "change-1",
            plan_path,
            actor_id="worker",
            context_id="worker-context",
            kind="agent",
        )
        _state, assignment = start_plan_decision_review(
            root,
            project,
            "change-1",
            actor_id="decision-reviewer",
            context_id="fresh-plan-decision-context",
            kind="agent",
            method="isolated-context",
            attested_by="test-host",
            evidence="The host created a fresh read-only reviewer context.",
        )
        return project, assignment

    def prepare_verified_authored_plan_decision(
        self, root: Path, inputs: Path
    ) -> Project:
        project, assignment = self.prepare_authored_plan_decision(root, inputs)
        review_path = inputs / "plan-decision-review.json"
        self.write_plan_decision_review(review_path, assignment)
        submit_plan_decision_review(root, project, "change-1", review_path)
        begin_implementation(
            root,
            "change-1",
            project=project,
            actor_id="worker",
            context_id="worker-context",
            kind="agent",
        )
        for profile in ("development", "review"):
            verify_change(
                root,
                project,
                "change-1",
                profile,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
        return project

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

    def write_shared_improvement_chain(
        self,
        inputs: Path,
        signal: dict[str, object],
        *,
        prefix: str,
    ) -> dict[str, Path]:
        target = signal["target"]
        source = signal["source"]
        claim = signal["claim"]
        assert isinstance(target, dict)
        assert isinstance(source, dict)
        assert isinstance(claim, dict)
        catalog = {
            "schemaVersion": 1,
            "kind": "engineering-process-improvement-catalog",
            "producer": target,
            "entries": [],
        }
        disposition = {
            "schemaVersion": 1,
            "kind": "engineering-process-improvement-disposition",
            "createdAt": "2026-08-24T01:00:00Z",
            "signalSha256": canonical_json_digest(signal),
            "catalogSha256": canonical_json_digest(catalog),
            "catalogStatus": "absent",
            "producer": {
                **target,
                "checkpoint": "9" * 40,
                "process": {
                    "version": "0.4.0",
                    "digest": f"sha256:{'a' * 64}",
                },
            },
            "decision": "accepted",
            "ownerBoundary": claim["ownerBoundary"],
            "reusableClass": claim["reusableClass"],
            "canonicalInvariantId": claim["proposedInvariantId"],
            "recurrence": "new",
            "linkedChangeId": "producer-change",
            "rationaleSha256": claim["rationaleSha256"],
            "exception": None,
            "requiredProof": {
                "producerLifecycle": True,
                "immutableRelease": True,
                "consumerReproduction": True,
            },
            "controls": {
                "grantsImplementation": False,
                "grantsMerge": False,
                "grantsRelease": False,
                "grantsAdoption": False,
            },
        }
        version = source["process"]["version"]
        release = {
            "repository": target["repository"],
            "version": version,
            "tag": f"v{version}",
            "releaseName": f"v{version}",
            "commit": "e" * 40,
            "artifactSetSha256": f"sha256:{'f' * 64}",
        }
        resolution = {
            "schemaVersion": 1,
            "kind": "engineering-process-improvement-resolution",
            "resolvedAt": "2026-08-24T02:00:00Z",
            "signalSha256": canonical_json_digest(signal),
            "dispositionSha256": canonical_json_digest(disposition),
            "canonicalInvariantId": disposition["canonicalInvariantId"],
            "producerLifecycle": {
                "project": target["project"],
                "changeId": disposition["linkedChangeId"],
                "checkpoint": "c" * 40,
                "receiptSha256": f"sha256:{'d' * 64}",
            },
            "release": release,
            "regressionEvidence": [f"sha256:{'1' * 64}"],
        }
        reproduction = {
            "schemaVersion": 1,
            "kind": "engineering-process-improvement-reproduction",
            "completedAt": "2026-08-24T03:00:00Z",
            "signalSha256": canonical_json_digest(signal),
            "dispositionSha256": canonical_json_digest(disposition),
            "resolutionSha256": canonical_json_digest(resolution),
            "canonicalInvariantId": disposition["canonicalInvariantId"],
            "consumer": {
                "project": source["project"],
                "repository": source["repository"],
                "checkpoint": source["checkpoint"],
                "workspaceFingerprint": source["workspaceFingerprint"],
                "process": source["process"],
            },
            "release": release,
            "evidence": {
                "kind": "lifecycle-receipt",
                "status": "passed",
                "artifactSha256": f"sha256:{'2' * 64}",
                "artifactBytes": 4096,
                "changeId": "consumer-reproduction",
                "cycle": 1,
                "profiles": ["development", "review"],
                "reference": None,
            },
        }
        documents = {
            "signal": signal,
            "catalog": catalog,
            "disposition": disposition,
            "resolution": resolution,
            "reproduction": reproduction,
        }
        paths: dict[str, Path] = {}
        for name, document in documents.items():
            path = inputs / f"{prefix}-{name}.json"
            path.write_text(json.dumps(document) + "\n", encoding="utf-8")
            paths[name] = path
        return paths

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

    def test_authored_plan_requires_fresh_clear_assessment_before_implementation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            project, assignment = self.prepare_authored_plan_decision(root, inputs)

            with self.assertRaisesRegex(ContractError, "review artifact is missing"):
                begin_implementation(
                    root,
                    "change-1",
                    project=project,
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )

            review_path = inputs / "plan-decision-review.json"
            self.write_plan_decision_review(review_path, assignment)
            submit_plan_decision_review(
                root, project, "change-1", review_path
            )
            with self.assertRaisesRegex(ContractError, "read-only plan reviewer"):
                begin_implementation(
                    root,
                    "change-1",
                    project=project,
                    actor_id="decision-reviewer",
                    context_id="fresh-plan-decision-context",
                    kind="agent",
                )
            with self.assertRaisesRegex(ContractError, "read-only plan reviewer"):
                begin_implementation(
                    root,
                    "change-1",
                    project=project,
                    actor_id="different-reviewer-name",
                    context_id="fresh-plan-decision-context",
                    kind="agent",
                )
            with self.assertRaisesRegex(ContractError, "current project contract"):
                begin_implementation(
                    root,
                    "change-1",
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )
            state = begin_implementation(
                root,
                "change-1",
                project=project,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )

            self.assertEqual("implementing", state["phase"])
            self.assertTrue(state["planDecision"]["authorized"])

    def test_authored_plan_assessment_fails_closed_on_source_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            project, assignment = self.prepare_authored_plan_decision(root, inputs)
            review_path = inputs / "plan-decision-review.json"
            self.write_plan_decision_review(review_path, assignment)
            submit_plan_decision_review(root, project, "change-1", review_path)
            (root / "tracked.txt").write_text("drifted\n", encoding="utf-8")

            with self.assertRaisesRegex(ContractError, "stale.*implementation source"):
                begin_implementation(
                    root,
                    "change-1",
                    project=project,
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )

    def test_verified_source_drift_requires_refreshed_plan_assessment(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            project = self.prepare_verified_authored_plan_decision(root, inputs)
            (root / "tracked.txt").write_text("new checkpoint\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "fix: create new implementation source"],
                cwd=root,
                check=True,
            )

            with self.assertRaisesRegex(ContractError, "assignment cycle is stale"):
                begin_implementation(
                    root,
                    "change-1",
                    project=project,
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )
            _state, assignment = start_plan_decision_review(
                root,
                project,
                "change-1",
                actor_id="decision-reviewer-two",
                context_id="fresh-plan-decision-context-two",
                kind="agent",
                method="isolated-context",
                attested_by="test-host",
                evidence="The host created a new read-only context for cycle two.",
            )
            review_path = inputs / "plan-decision-review-2.json"
            self.write_plan_decision_review(review_path, assignment)
            submit_plan_decision_review(root, project, "change-1", review_path)

            state = begin_implementation(
                root,
                "change-1",
                project=project,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )

            self.assertEqual(2, state["cycle"])
            self.assertEqual("implementing", state["phase"])

    def test_review_finding_cycle_requires_refreshed_plan_assessment(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            project = self.prepare_verified_authored_plan_decision(root, inputs)
            _state, assignment = start_review(
                root,
                "change-1",
                actor_id="final-reviewer",
                context_id="fresh-final-review-context",
                kind="agent",
                method="isolated-context",
                attested_by="test-host",
                evidence="The host created a fresh final review context.",
            )
            report_path = inputs / "changes-requested-review.json"
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
                                "id": "cycle-two-finding",
                                "severity": "high",
                                "path": "tracked.txt",
                                "line": 1,
                                "summary": "The implementation needs correction",
                                "evidence": "The exact checkpoint retains the defect.",
                                "status": "open",
                                "resolutionEvidence": None,
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            submit_review(root, "change-1", report_path)
            state = load_state(root, "change-1")
            classify_improvement_case(
                root,
                "change-1",
                state["improvements"][0]["id"],
                owner_boundary="project-local",
                reusable_class="local-behavior",
                invariant_id="cycle-two-correction",
                disposition="local-fix",
                rationale_sha256=f"sha256:{'b' * 64}",
                target_project=None,
                target_repository=None,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )

            with self.assertRaisesRegex(ContractError, "assignment cycle is stale"):
                begin_implementation(
                    root,
                    "change-1",
                    project=project,
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )
            _state, decision_assignment = start_plan_decision_review(
                root,
                project,
                "change-1",
                actor_id="decision-reviewer-two",
                context_id="fresh-finding-cycle-decision-context",
                kind="agent",
                method="isolated-context",
                attested_by="test-host",
                evidence="The host created a fresh read-only finding-cycle context.",
            )
            decision_review_path = inputs / "finding-cycle-decision-review.json"
            self.write_plan_decision_review(
                decision_review_path, decision_assignment
            )
            submit_plan_decision_review(
                root, project, "change-1", decision_review_path
            )

            state = begin_implementation(
                root,
                "change-1",
                project=project,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            self.assertEqual(2, state["cycle"])

    def test_authored_plan_decision_evidence_survives_completion_export(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            project, decision_assignment = self.prepare_authored_plan_decision(
                root, inputs
            )
            decision_review_path = inputs / "plan-decision-review.json"
            self.write_plan_decision_review(
                decision_review_path, decision_assignment
            )
            submit_plan_decision_review(
                root, project, "change-1", decision_review_path
            )
            begin_implementation(
                root,
                "change-1",
                project=project,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            for profile in ("development", "review"):
                verify_change(
                    root,
                    project,
                    "change-1",
                    profile,
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )
            _state, assignment = start_review(
                root,
                "change-1",
                actor_id="final-reviewer",
                context_id="fresh-final-review-context",
                kind="agent",
                method="isolated-context",
                attested_by="test-host",
                evidence="The host created a fresh final semantic review context.",
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
                        "quality": self.review_quality(),
                        "verdict": "approved",
                        "findings": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            submit_review(root, "change-1", report_path)
            finish_change(
                root,
                "change-1",
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            receipt_path = inputs / "receipt.json"

            exported = export_receipt(root, "change-1", receipt_path)

            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "authored", receipt["artifacts"]["planDecision"]["kind"]
            )
            self.assertIsNotNone(
                receipt["artifacts"]["planDecision"]["contextReservation"]
            )
            self.assertEqual(exported, validate_receipt(receipt_path))
            missing_path = inputs / "missing-plan-decision-receipt.json"
            missing = json.loads(receipt_path.read_text(encoding="utf-8"))
            del missing["artifacts"]["planDecision"]
            missing["state"]["document"]["planDecision"] = None
            missing["state"]["canonicalDigest"] = _canonical_digest(
                missing["state"]["document"]
            )
            missing_path.write_text(json.dumps(missing) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                ContractError, "schema-3 plan is missing required plan decision"
            ):
                validate_receipt(missing_path)

    def test_decision_required_assessment_cannot_implement_without_owner_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            project, assignment = self.prepare_authored_plan_decision(root, inputs)
            review_path = inputs / "plan-decision-review.json"
            self.write_plan_decision_review(
                review_path, assignment, decision_category="architecture"
            )
            submit_plan_decision_review(root, project, "change-1", review_path)

            with self.assertRaisesRegex(ContractError, "complete owner-decision chain"):
                begin_implementation(
                    root,
                    "change-1",
                    project=project,
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )

    def test_decision_required_assessment_reuses_exact_owner_resolution_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            project, assignment = self.prepare_authored_plan_decision(root, inputs)
            assessment_path = inputs / "plan-decision-review.json"
            self.write_plan_decision_review(
                assessment_path, assignment, decision_category="architecture"
            )
            _state, assessment = submit_plan_decision_review(
                root, project, "change-1", assessment_path
            )
            recommendation = json.loads(
                (
                    Path(__file__).resolve().parent.parent
                    / "examples"
                    / "recommendation.json"
                ).read_text(encoding="utf-8")
            )
            assessment_digest = canonical_json_digest(assessment)
            recommendation["decisionId"] = "change-1"
            recommendation["coordinator"] = {
                "actorId": "worker",
                "contextId": "worker-context",
                "kind": "agent",
            }
            binding = {
                "id": "plan-decision-assessment",
                "statement": "The recommendation is scoped to the exact fresh material-decision assessment for this authored plan.",
                "source": f"plan-decision-review:{assessment_digest}",
                "evidenceSha256": assessment_digest,
            }
            recommendation["invariants"].insert(1, binding)
            for option in recommendation["options"]:
                option["invariantAssessments"].insert(
                    1,
                    {
                        "invariantId": "plan-decision-assessment",
                        "status": "satisfied",
                        "evidenceSha256": assessment_digest,
                    },
                )
            recommendation_path = inputs / "recommendation.json"
            recommendation_path.write_text(
                json.dumps(recommendation) + "\n", encoding="utf-8"
            )
            assignment_result = start_recommendation_review(
                root,
                recommendation_path,
                actor_id="recommendation-reviewer",
                context_id="fresh-recommendation-context",
                kind="agent",
                method="isolated-context",
                attested_by="test-host",
                evidence="The host created another fresh read-only context.",
            )
            recommendation_assignment_path = root / assignment_result["assignment"]
            recommendation_review = json.loads(
                (
                    Path(__file__).resolve().parent.parent
                    / "examples"
                    / "recommendation-review.json"
                ).read_text(encoding="utf-8")
            )
            recommendation_review["decisionId"] = "change-1"
            recommendation_review["recommendationSha256"] = canonical_json_digest(
                recommendation
            )
            recommendation_assignment = json.loads(
                recommendation_assignment_path.read_text(encoding="utf-8")
            )
            recommendation_review["assignmentSha256"] = canonical_json_digest(
                recommendation_assignment
            )
            recommendation_review["reviewer"] = recommendation_assignment["reviewer"]
            recommendation_review["invariantAssessments"].insert(
                1,
                {
                    "invariantId": "plan-decision-assessment",
                    "status": "verified",
                    "evidence": "The recommendation is bound to the exact assessment.",
                },
            )
            recommendation_review_path = inputs / "recommendation-review.json"
            recommendation_review_path.write_text(
                json.dumps(recommendation_review) + "\n", encoding="utf-8"
            )
            resolution_path = inputs / "recommendation-resolution.json"
            create_recommendation_resolution(
                root,
                recommendation_path,
                recommendation_assignment_path,
                recommendation_review_path,
                selected_option_id="verify-before-completion",
                owner_id="project-owner",
                owner_evidence_sha256=f"sha256:{'9' * 64}",
                selection_rationale_sha256=f"sha256:{'a' * 64}",
                output=resolution_path,
            )
            resolve_plan_decision(
                root,
                project,
                "change-1",
                recommendation_path=recommendation_path,
                assignment_path=recommendation_assignment_path,
                review_path=recommendation_review_path,
                resolution_path=resolution_path,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )

            state = begin_implementation(
                root,
                "change-1",
                project=project,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            self.assertEqual("implementing", state["phase"])
            self.assertTrue(state["planDecision"]["authorized"])

    def test_third_changes_requested_cycle_requires_owner_resolution_before_fourth(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            project = self.prepare_verified_authored_plan_decision(root, inputs)
            pending: list[dict[str, object]] = []

            for expected_cycle in range(1, 4):
                _state, assignment = start_review(
                    root,
                    "change-1",
                    actor_id=f"final-reviewer-{expected_cycle}",
                    context_id=f"fresh-final-review-context-{expected_cycle}",
                    kind="agent",
                    method="isolated-context",
                    attested_by="test-host",
                    evidence="The host created a fresh final review context.",
                )
                carried = []
                for finding in pending:
                    resolved = dict(finding)
                    resolved["status"] = "resolved"
                    resolved["resolutionEvidence"] = (
                        f"Cycle {expected_cycle} regression resolves the prior finding."
                    )
                    carried.append(resolved)
                current = {
                    "id": f"renamed-boundary-finding-{expected_cycle}",
                    "severity": "high",
                    "path": "tracked.txt",
                    "line": 1,
                    "summary": "The same correction boundary still needs work",
                    "evidence": f"Cycle {expected_cycle} exposes the next failure.",
                    "status": "open",
                    "resolutionEvidence": None,
                }
                findings = [*carried, current]
                report_path = inputs / f"review-{expected_cycle}.json"
                report_path.write_text(
                    json.dumps(
                        {
                            "schemaVersion": 3,
                            "changeId": "change-1",
                            "cycle": expected_cycle,
                            "checkpoint": assignment["checkpoint"],
                            "workspaceFingerprint": assignment[
                                "workspaceFingerprint"
                            ],
                            "comparisonBase": assignment["comparisonBase"],
                            "reviewer": assignment["reviewer"],
                            "independence": assignment["independence"],
                            "quality": self.review_quality(
                                failed=("correctness",)
                            ),
                            "verdict": "changes-requested",
                            "findings": findings,
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                state = submit_review(root, "change-1", report_path)
                self.assertEqual(
                    list(range(1, expected_cycle + 1)),
                    state["reviewLoop"]["window"]["reviewCycles"],
                )
                for case in state["improvements"]:
                    if case["phase"] != "classification-required":
                        continue
                    classify_improvement_case(
                        root,
                        "change-1",
                        case["id"],
                        owner_boundary="project-local",
                        reusable_class="local-behavior",
                        invariant_id=f"renamed-invariant-{expected_cycle}",
                        disposition="local-fix",
                        rationale_sha256=f"sha256:{str(expected_cycle) * 64}",
                        target_project=None,
                        target_repository=None,
                        actor_id="worker",
                        context_id="worker-context",
                        kind="agent",
                    )
                pending = [current]
                if expected_cycle == 3:
                    break
                _state, decision_assignment = start_plan_decision_review(
                    root,
                    project,
                    "change-1",
                    actor_id=f"decision-reviewer-{expected_cycle + 1}",
                    context_id=f"fresh-decision-context-{expected_cycle + 1}",
                    kind="agent",
                    method="isolated-context",
                    attested_by="test-host",
                    evidence="The host created a fresh plan decision context.",
                )
                decision_review_path = inputs / f"decision-{expected_cycle + 1}.json"
                self.write_plan_decision_review(
                    decision_review_path, decision_assignment
                )
                submit_plan_decision_review(
                    root, project, "change-1", decision_review_path
                )
                begin_implementation(
                    root,
                    "change-1",
                    project=project,
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )
                for profile in ("development", "review"):
                    verify_change(
                        root,
                        project,
                        "change-1",
                        profile,
                        actor_id="worker",
                        context_id="worker-context",
                        kind="agent",
                    )

            state = load_state(root, "change-1")
            escalation = state["reviewLoop"]["escalations"][0]
            self.assertEqual("cycle-limit", escalation["kind"])
            self.assertEqual([1, 2, 3], escalation["reviewCycles"])
            self.assertEqual("decision-required", escalation["status"])

            _state, decision_assignment = start_plan_decision_review(
                root,
                project,
                "change-1",
                actor_id="decision-reviewer-four",
                context_id="fresh-decision-context-four",
                kind="agent",
                method="isolated-context",
                attested_by="test-host",
                evidence="The host created the owner-escalation plan context.",
            )
            self.assertEqual(
                escalation["id"],
                decision_assignment["reviewLoopEscalation"]["id"],
            )
            clear_path = inputs / "clear-loop-decision.json"
            self.write_plan_decision_review(clear_path, decision_assignment)
            with self.assertRaisesRegex(ContractError, "decision-required"):
                submit_plan_decision_review(
                    root, project, "change-1", clear_path
                )
            assessment_path = inputs / "loop-decision.json"
            self.write_plan_decision_review(
                assessment_path,
                decision_assignment,
                decision_category="architecture",
            )
            _state, assessment = submit_plan_decision_review(
                root, project, "change-1", assessment_path
            )
            state = self.resolve_current_plan_decision_with_effect(
                root,
                inputs,
                project,
                assessment,
                lifecycle_effect="resume-correction-window",
                prefix="loop",
            )
            escalation = state["reviewLoop"]["escalations"][0]
            self.assertEqual("resolved", escalation["status"])
            self.assertEqual(2, state["reviewLoop"]["window"]["number"])
            self.assertEqual(4, state["reviewLoop"]["window"]["startedAtCycle"])
            self.assertEqual([], state["reviewLoop"]["window"]["reviewCycles"])

            state = begin_implementation(
                root,
                "change-1",
                project=project,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            self.assertEqual(4, state["cycle"])
            self.assertEqual("implementing", state["phase"])
            for profile in ("development", "review"):
                verify_change(
                    root,
                    project,
                    "change-1",
                    profile,
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )
            _state, final_assignment = start_review(
                root,
                "change-1",
                actor_id="final-reviewer-four",
                context_id="fresh-final-review-context-four",
                kind="agent",
                method="isolated-context",
                attested_by="test-host",
                evidence="The host created the terminal final review context.",
            )
            resolved_finding = dict(pending[0])
            resolved_finding["status"] = "resolved"
            resolved_finding["resolutionEvidence"] = (
                "Cycle four regression resolves the carried finding."
            )
            final_review_path = inputs / "final-review-four.json"
            final_review_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 3,
                        "changeId": "change-1",
                        "cycle": 4,
                        "checkpoint": final_assignment["checkpoint"],
                        "workspaceFingerprint": final_assignment[
                            "workspaceFingerprint"
                        ],
                        "comparisonBase": final_assignment["comparisonBase"],
                        "reviewer": final_assignment["reviewer"],
                        "independence": final_assignment["independence"],
                        "quality": self.review_quality(),
                        "verdict": "approved",
                        "findings": [resolved_finding],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            submit_review(root, "change-1", final_review_path)
            state, completion = finish_change(
                root,
                "change-1",
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            self.assertEqual(state["reviewLoop"], completion["reviewLoop"])
            receipt_path = inputs / "review-loop-receipt.json"
            exported = export_receipt(root, "change-1", receipt_path)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(
                escalation["id"], receipt["artifacts"]["reviewLoop"][0]["id"]
            )
            self.assertEqual(exported, validate_receipt(receipt_path))

    def test_finite_contract_gap_escalates_on_first_review_and_cannot_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            project = self.plan_decision_project()
            contract_path = inputs / "contract.json"
            plan_path = inputs / "plan.json"
            self.write_finite_contract(contract_path)
            state = start_change(
                root,
                project,
                contract_path,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            self.write_authored_plan(plan_path, state["contract"]["digest"])
            register_plan(
                root,
                project,
                "change-1",
                plan_path,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            _state, plan_assignment = start_plan_decision_review(
                root,
                project,
                "change-1",
                actor_id="plan-reviewer",
                context_id="fresh-plan-context",
                kind="agent",
                method="isolated-context",
                attested_by="test-host",
                evidence="The host created a fresh plan context.",
            )
            plan_review_path = inputs / "plan-review.json"
            self.write_plan_decision_review(plan_review_path, plan_assignment)
            submit_plan_decision_review(
                root, project, "change-1", plan_review_path
            )
            begin_implementation(
                root,
                "change-1",
                project=project,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            for profile in ("development", "review"):
                verify_change(
                    root,
                    project,
                    "change-1",
                    profile,
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )
            _state, review_assignment = start_review(
                root,
                "change-1",
                actor_id="final-reviewer",
                context_id="fresh-final-context",
                kind="agent",
                method="isolated-context",
                attested_by="test-host",
                evidence="The host created a fresh final context.",
            )
            report_path = inputs / "gap-review.json"
            report_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 3,
                        "changeId": "change-1",
                        "cycle": 1,
                        "checkpoint": review_assignment["checkpoint"],
                        "workspaceFingerprint": review_assignment[
                            "workspaceFingerprint"
                        ],
                        "comparisonBase": review_assignment["comparisonBase"],
                        "reviewer": review_assignment["reviewer"],
                        "independence": review_assignment["independence"],
                        "quality": self.review_quality(
                            failed=("correctness",)
                        ),
                        "verdict": "changes-requested",
                        "findings": [
                            {
                                "id": "outside-finite-model",
                                "severity": "high",
                                "path": "tracked.txt",
                                "line": 1,
                                "summary": "The observed failure is outside the model",
                                "evidence": "No registered fault row covers it.",
                                "boundaryStatus": "contract-gap",
                                "trustBoundaryId": None,
                                "faultRowId": None,
                                "criterionIds": [],
                                "status": "open",
                                "resolutionEvidence": None,
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            state = submit_review(root, "change-1", report_path)
            escalation = state["reviewLoop"]["escalations"][0]
            self.assertEqual("contract-gap", escalation["kind"])
            self.assertEqual([1], escalation["reviewCycles"])
            case = state["improvements"][0]
            classify_improvement_case(
                root,
                "change-1",
                case["id"],
                owner_boundary="project-local",
                reusable_class="local-behavior",
                invariant_id="outside-finite-model",
                disposition="local-fix",
                rationale_sha256=f"sha256:{'1' * 64}",
                target_project=None,
                target_repository=None,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            with self.assertRaisesRegex(ContractError, "owner decision"):
                begin_implementation(
                    root,
                    "change-1",
                    project=project,
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )
            _state, escalation_assignment = start_plan_decision_review(
                root,
                project,
                "change-1",
                actor_id="gap-plan-reviewer",
                context_id="fresh-gap-plan-context",
                kind="agent",
                method="isolated-context",
                attested_by="test-host",
                evidence="The host created a fresh contract-gap context.",
            )
            self.assertEqual(
                "contract-gap",
                escalation_assignment["reviewLoopEscalation"]["kind"],
            )
            clear_path = inputs / "gap-clear-review.json"
            self.write_plan_decision_review(clear_path, escalation_assignment)
            with self.assertRaisesRegex(ContractError, "decision-required"):
                submit_plan_decision_review(
                    root, project, "change-1", clear_path
                )

            assessment_path = inputs / "gap-decision-review.json"
            self.write_plan_decision_review(
                assessment_path,
                escalation_assignment,
                decision_category="scope",
            )
            _state, assessment = submit_plan_decision_review(
                root, project, "change-1", assessment_path
            )
            state = self.resolve_current_plan_decision_with_effect(
                root,
                inputs,
                project,
                assessment,
                lifecycle_effect="supersede-change",
                prefix="gap",
            )
            self.assertEqual(
                "superseded",
                state["reviewLoop"]["escalations"][0]["status"],
            )
            with self.assertRaisesRegex(ContractError, "permanently stopped"):
                begin_implementation(
                    root,
                    "change-1",
                    project=project,
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )

    def test_finite_contract_requires_owner_decision_policy_before_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            inputs = Path(directory) / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            contract_path = inputs / "contract.json"
            self.write_finite_contract(contract_path)
            with self.assertRaisesRegex(ContractError, "plan decision policy"):
                start_change(
                    root,
                    self.project(),
                    contract_path,
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )

    def test_finite_contract_rejects_plan_without_authored_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            inputs = Path(directory) / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            project = self.plan_decision_project()
            contract_path = inputs / "contract.json"
            plan_path = inputs / "plan.json"
            self.write_finite_contract(contract_path)
            state = start_change(
                root,
                project,
                contract_path,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            self.write_plan(plan_path, state["contract"]["digest"])
            with self.assertRaisesRegex(
                ContractError, "schema-4 changes require a bounded schema-3 plan"
            ):
                register_plan(
                    root,
                    project,
                    "change-1",
                    plan_path,
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )

    def test_opted_in_project_rejects_legacy_plan_without_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            project = self.plan_decision_project()
            contract_path = inputs / "contract.json"
            plan_path = inputs / "plan.json"
            self.write_contract(contract_path)
            state = start_change(
                root,
                project,
                contract_path,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            self.write_plan(plan_path, state["contract"]["digest"])

            with self.assertRaisesRegex(ContractError, "schema-3 plan"):
                register_plan(
                    root,
                    project,
                    "change-1",
                    plan_path,
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )

    def test_non_opted_project_resumes_historical_schema_one_plan(self):
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
            run_root = root / ".process" / "runs" / "change-1"
            registered_contract_path = run_root / "contract.json"
            registered_contract = json.loads(
                registered_contract_path.read_text(encoding="utf-8")
            )
            registered_contract["schemaVersion"] = 2
            del registered_contract["quality"]
            registered_contract_path.write_text(
                json.dumps(registered_contract, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            registered_plan_path = run_root / "plan.json"
            registered_plan = json.loads(
                registered_plan_path.read_text(encoding="utf-8")
            )
            registered_plan["schemaVersion"] = 1
            registered_plan_path.write_text(
                json.dumps(registered_plan, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            state_path = run_root / "state.json"
            historical_state = json.loads(state_path.read_text(encoding="utf-8"))
            historical_state.pop("reviewLoop", None)
            historical_state["contract"]["digest"] = (
                "sha256:"
                + hashlib.sha256(registered_contract_path.read_bytes()).hexdigest()
            )
            historical_state["plan"]["digest"] = (
                "sha256:"
                + hashlib.sha256(registered_plan_path.read_bytes()).hexdigest()
            )
            state_path.write_text(
                json.dumps(historical_state, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            state = begin_implementation(
                root,
                "change-1",
                project=self.project(),
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )

            self.assertEqual("implementing", state["phase"])
            self.assertNotIn("reviewLoop", state)

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

    def test_stale_verified_source_can_begin_a_new_implementation_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            self.prepare_verified_change(root, inputs)

            (root / "tracked.txt").write_text("remote CI correction\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "fix: address remote verification"],
                cwd=root,
                check=True,
            )

            state = begin_implementation(
                root,
                "change-1",
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )

            self.assertEqual("implementing", state["phase"])
            self.assertEqual(2, state["cycle"])
            self.assertEqual([], state["verification"])
            invalidated = [
                event
                for event in state["history"]
                if event["event"] == "verification-invalidated"
            ]
            self.assertEqual(1, len(invalidated))
            self.assertEqual(1, invalidated[0]["previousCycle"])
            self.assertEqual(
                "source-changed-after-verification", invalidated[0]["reason"]
            )
            self.assertEqual(2, len(invalidated[0]["previousVerification"]))

    def test_current_verified_source_cannot_bypass_independent_review(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            self.prepare_verified_change(root, inputs)

            with self.assertRaisesRegex(
                ContractError, "must enter independent review"
            ):
                begin_implementation(
                    root,
                    "change-1",
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )

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
            bootstrap_path = inputs / "bootstrap-authorization.json"
            bootstrap = export_bootstrap_authorization(
                root, "change-1", bootstrap_path
            )
            self.assertEqual(
                bootstrap, validate_bootstrap_authorization(bootstrap_path)
            )
            with self.assertRaisesRegex(ContractError, "unsupported"):
                validate_receipt(bootstrap_path)
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
            self.assertEqual(1, len(state["improvements"]))
            case = state["improvements"][0]
            self.assertEqual("classification-required", case["phase"])
            with self.assertRaisesRegex(
                ContractError, "improvement-required"
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
            state = classify_improvement_case(
                root,
                "change-1",
                case["id"],
                owner_boundary="project-local",
                reusable_class="local-behavior",
                invariant_id="clean-verification-checkpoint",
                disposition="local-fix",
                rationale_sha256=f"sha256:{'a' * 64}",
                target_project=None,
                target_repository=None,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            self.assertEqual(
                "local-resolution-required", state["improvements"][0]["phase"]
            )

    def test_shared_failure_exports_untrusted_signal_and_blocks_candidate(self):
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

            def failed_report(*args, **kwargs):
                report = real_run_profile(*args, **kwargs)
                report["status"] = "failed"
                report["checks"][0]["status"] = "failed"
                report["checks"][0]["exitCode"] = 7
                return report

            with (
                mock.patch.object(
                    lifecycle_module,
                    "run_profile",
                    side_effect=failed_report,
                ),
                self.assertRaisesRegex(ContractError, "profile-status-not-passed"),
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
            case = state["improvements"][0]
            classify_improvement_case(
                root,
                "change-1",
                case["id"],
                owner_boundary="shared-process",
                reusable_class="deterministic-enforcement",
                invariant_id="shared-verification-boundary",
                disposition="shared-escalation",
                rationale_sha256=f"sha256:{'d' * 64}",
                target_project="engineering-process",
                target_repository="example/engineering-process",
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            examples = Path(__file__).resolve().parent.parent / "examples"
            with self.assertRaisesRegex(ContractError, "cannot advance consumer case"):
                attach_improvement_chain(
                    root,
                    "change-1",
                    case["id"],
                    signal_path=examples / "improvement-signal.json",
                    disposition_path=examples / "improvement-disposition.json",
                    resolution_path=examples / "improvement-resolution.json",
                    reproduction_path=examples / "improvement-reproduction.json",
                    catalog_path=examples / "improvement-catalog.json",
                    actor_id="worker",
                    context_id="worker-context",
                    actor_kind="agent",
                )
            signal_path = inputs / "signal.json"
            result = export_improvement_signal(
                root,
                "change-1",
                case["id"],
                source_repository="example/sample-project",
                affected_surfaces=["verification"],
                reference=None,
                output=signal_path,
                actor_id="worker",
                context_id="worker-context",
                actor_kind="agent",
            )
            signal = json.loads(signal_path.read_text(encoding="utf-8"))

            self.assertEqual("producer-disposition-required", result["phase"])
            self.assertEqual(False, signal["controls"]["rawOutputIncluded"])
            self.assertNotIn("command", signal["evidence"])
            with self.assertRaisesRegex(ContractError, "improvement-pending"):
                verify_change(
                    root,
                    self.project(),
                    "change-1",
                    "development",
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )
            status = lifecycle_status(root, "change-1")
            self.assertEqual(1, status["improvementStatus"]["openCount"])
            self.assertEqual(
                "engineering-process",
                status["improvementStatus"]["blockers"][0]["nextOwner"],
            )
            wrong_invariant = json.loads(json.dumps(signal))
            wrong_invariant["claim"][
                "proposedInvariantId"
            ] = "wrong-verification-boundary"
            wrong_invariant_paths = self.write_shared_improvement_chain(
                inputs, wrong_invariant, prefix="wrong-invariant"
            )
            with self.assertRaisesRegex(
                ContractError, "does not belong to lifecycle case"
            ):
                attach_improvement_chain(
                    root,
                    "change-1",
                    case["id"],
                    signal_path=wrong_invariant_paths["signal"],
                    disposition_path=wrong_invariant_paths["disposition"],
                    resolution_path=None,
                    reproduction_path=None,
                    catalog_path=wrong_invariant_paths["catalog"],
                    actor_id="worker",
                    context_id="worker-context",
                    actor_kind="agent",
                )
            wrong_target = json.loads(json.dumps(signal))
            wrong_target["target"] = {
                "project": "other-producer",
                "repository": "example/other-producer",
            }
            wrong_target_paths = self.write_shared_improvement_chain(
                inputs, wrong_target, prefix="wrong-target"
            )
            with self.assertRaisesRegex(
                ContractError, "does not belong to lifecycle case"
            ):
                attach_improvement_chain(
                    root,
                    "change-1",
                    case["id"],
                    signal_path=wrong_target_paths["signal"],
                    disposition_path=wrong_target_paths["disposition"],
                    resolution_path=None,
                    reproduction_path=None,
                    catalog_path=wrong_target_paths["catalog"],
                    actor_id="worker",
                    context_id="worker-context",
                    actor_kind="agent",
                )
            chain_paths = self.write_shared_improvement_chain(
                inputs, signal, prefix="matching"
            )
            validated = validate_improvement_chain(
                chain_paths["signal"],
                chain_paths["disposition"],
                catalog_path=chain_paths["catalog"],
            )
            original_disposition = chain_paths["disposition"].read_text(
                encoding="utf-8"
            )
            changed_disposition = json.loads(original_disposition)
            changed_disposition["rationaleSha256"] = f"sha256:{'3' * 64}"
            chain_paths["disposition"].write_text(
                json.dumps(changed_disposition) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ContractError, "changed after chain validation"):
                lifecycle_module.bind_improvement_chain(
                    root,
                    "change-1",
                    case["id"],
                    signal_path=chain_paths["signal"],
                    catalog_path=chain_paths["catalog"],
                    disposition_path=chain_paths["disposition"],
                    resolution_path=None,
                    reproduction_path=None,
                    expected_canonical_digests={
                        "signal": validated["signalSha256"],
                        "catalog": canonical_json_digest(
                            json.loads(
                                chain_paths["catalog"].read_text(encoding="utf-8")
                            )
                        ),
                        "disposition": validated["dispositionSha256"],
                    },
                    chain_phase=validated["phase"],
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )
            chain_paths["disposition"].write_text(
                original_disposition, encoding="utf-8"
            )
            attached = attach_improvement_chain(
                root,
                "change-1",
                case["id"],
                signal_path=chain_paths["signal"],
                disposition_path=chain_paths["disposition"],
                resolution_path=None,
                reproduction_path=None,
                catalog_path=chain_paths["catalog"],
                actor_id="worker",
                context_id="worker-context",
                actor_kind="agent",
            )
            self.assertEqual("producer-resolution-required", attached["casePhase"])
            disposition_status = lifecycle_status(root, "change-1")[
                "improvementStatus"
            ]["cases"][0]
            self.assertEqual("new", disposition_status["recurrence"])
            self.assertEqual(
                "producer-change",
                disposition_status["producer"]["linkedChangeId"],
            )
            with self.assertRaisesRegex(ContractError, "cannot advance consumer case"):
                attach_improvement_chain(
                    root,
                    "change-1",
                    case["id"],
                    signal_path=chain_paths["signal"],
                    disposition_path=chain_paths["disposition"],
                    resolution_path=None,
                    reproduction_path=None,
                    catalog_path=chain_paths["catalog"],
                    actor_id="worker",
                    context_id="worker-context",
                    actor_kind="agent",
                )
            released = attach_improvement_chain(
                root,
                "change-1",
                case["id"],
                signal_path=chain_paths["signal"],
                disposition_path=chain_paths["disposition"],
                resolution_path=chain_paths["resolution"],
                reproduction_path=None,
                catalog_path=chain_paths["catalog"],
                actor_id="worker",
                context_id="worker-context",
                actor_kind="agent",
            )
            self.assertEqual("consumer-reproduction-required", released["casePhase"])
            closed = attach_improvement_chain(
                root,
                "change-1",
                case["id"],
                signal_path=chain_paths["signal"],
                disposition_path=chain_paths["disposition"],
                resolution_path=chain_paths["resolution"],
                reproduction_path=chain_paths["reproduction"],
                catalog_path=chain_paths["catalog"],
                actor_id="worker",
                context_id="worker-context",
                actor_kind="agent",
            )
            self.assertEqual("closed", closed["casePhase"])
            closed_status = lifecycle_status(root, "change-1")["improvementStatus"]
            self.assertEqual(0, closed_status["openCount"])
            closed_case = closed_status["cases"][0]
            self.assertTrue(closed_case["closed"])
            self.assertEqual("v0.1.1", closed_case["release"]["tag"])
            self.assertEqual(
                signal["source"]["checkpoint"],
                closed_case["consumer"]["checkpoint"],
            )
            self.assertNotIn(".process/runs", json.dumps(closed_case))

    def test_self_discovered_producer_improvement_requires_catalog_and_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "producer"
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
                actor_id="producer",
                context_id="producer-context",
                kind="agent",
            )
            self.write_plan(plan_path, state["contract"]["digest"])
            register_plan(
                root,
                self.project(),
                "change-1",
                plan_path,
                actor_id="producer",
                context_id="producer-context",
                kind="agent",
            )
            begin_implementation(
                root,
                "change-1",
                actor_id="producer",
                context_id="producer-context",
                kind="agent",
            )
            real_run_profile = lifecycle_module.run_profile

            def failed_report(*args, **kwargs):
                report = real_run_profile(*args, **kwargs)
                report["status"] = "failed"
                report["checks"][0]["status"] = "failed"
                report["checks"][0]["exitCode"] = 9
                return report

            with (
                mock.patch.object(
                    lifecycle_module,
                    "run_profile",
                    side_effect=failed_report,
                ),
                self.assertRaisesRegex(ContractError, "profile-status-not-passed"),
            ):
                verify_change(
                    root,
                    self.project(),
                    "change-1",
                    "development",
                    actor_id="producer",
                    context_id="producer-context",
                    kind="agent",
                )
            state = load_state(root, "change-1")
            case = state["improvements"][0]
            with self.assertRaisesRegex(
                ContractError, "shared-process owner boundary"
            ):
                classify_improvement_case(
                    root,
                    "change-1",
                    case["id"],
                    owner_boundary="project-local",
                    reusable_class="deterministic-enforcement",
                    invariant_id="producer-route-complete",
                    disposition="producer-improvement",
                    rationale_sha256=f"sha256:{'4' * 64}",
                    target_project=None,
                    target_repository=None,
                    actor_id="producer",
                    context_id="producer-context",
                    kind="agent",
                )
            state = classify_improvement_case(
                root,
                "change-1",
                case["id"],
                owner_boundary="shared-process",
                reusable_class="deterministic-enforcement",
                invariant_id="producer-route-complete",
                disposition="producer-improvement",
                rationale_sha256=f"sha256:{'4' * 64}",
                target_project=None,
                target_repository=None,
                actor_id="producer",
                context_id="producer-context",
                kind="agent",
            )
            case = state["improvements"][0]
            self.assertEqual("local", case["role"])
            self.assertEqual("local-resolution-required", case["phase"])
            with self.assertRaisesRegex(
                ContractError, "reviewed improvement-catalog.json"
            ):
                lifecycle_module._require_producer_catalog_activation(
                    root, state, case
                )
            catalog = {
                "schemaVersion": 1,
                "kind": "engineering-process-improvement-catalog",
                "producer": {
                    "project": "sample-project",
                    "repository": "example/sample-project",
                },
                "entries": [
                    {
                        "id": "producer-route-complete",
                        "reusableClass": "deterministic-enforcement",
                        "status": "active",
                        "publicSurfaces": ["lifecycle"],
                        "lastResolution": None,
                        "activeChangeId": "change-1",
                    }
                ],
            }
            (root / "improvement-catalog.json").write_text(
                json.dumps(catalog) + "\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "improvement-catalog.json"], cwd=root, check=True
            )
            subprocess.run(
                ["git", "commit", "-qm", "activate producer invariant"],
                cwd=root,
                check=True,
            )
            open_status = lifecycle_status(root, "change-1")["improvementStatus"][
                "cases"
            ][0]
            self.assertEqual("active", open_status["catalog"]["status"])
            self.assertEqual("change-1", open_status["catalog"]["activeChangeId"])
            self.assertEqual("new", open_status["recurrence"])
            self.assertEqual("sample-project", open_status["producer"]["project"])
            self.assertEqual(
                "example/sample-project", open_status["producer"]["repository"]
            )
            self.assertEqual(
                "change-1", open_status["producer"]["linkedChangeId"]
            )
            self.assertIn("catalog", open_status["artifacts"])
            self.assertNotIn("path", open_status["artifacts"]["catalog"])
            for profile in ("development", "review"):
                state, _report = verify_change(
                    root,
                    self.project(),
                    "change-1",
                    profile,
                    actor_id="producer",
                    context_id="producer-context",
                    kind="agent",
                )
            state, assignment = start_review(
                root,
                "change-1",
                actor_id="reviewer",
                context_id="producer-improvement-review",
                kind="agent",
                method="isolated-context",
                attested_by="test-host",
                evidence="The test host created a fresh read-only review context",
            )
            report_path = inputs / "review.json"
            report_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 3,
                        "changeId": "change-1",
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
                ),
                encoding="utf-8",
            )
            state = submit_review(root, "change-1", report_path)
            self.assertEqual("approved", state["phase"])
            self.assertEqual("closed", state["improvements"][0]["phase"])
            state, completion = finish_change(
                root,
                "change-1",
                actor_id="producer",
                context_id="producer-context",
                kind="agent",
            )
            self.assertEqual("completed", state["phase"])
            self.assertEqual("local", completion["improvements"][0]["role"])
            self.assertIsNone(
                completion["improvements"][0]["signalCanonicalSha256"]
            )
            self.assertEqual(
                canonical_json_digest(catalog),
                completion["improvements"][0]["catalogCanonicalSha256"],
            )
            closed_lifecycle_status = lifecycle_status(root, "change-1")
            self.assertEqual([], closed_lifecycle_status["issues"])
            closed_status = closed_lifecycle_status["improvementStatus"]["cases"][0]
            self.assertTrue(closed_status["closed"])
            self.assertEqual("active", closed_status["catalog"]["status"])
            self.assertEqual(
                "change-1", closed_status["producer"]["linkedChangeId"]
            )

    def test_producer_ingests_only_disposition_linked_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "producer"
            inputs = base / "inputs"
            root.mkdir()
            inputs.mkdir()
            self.initialize_repository(root)
            (root / ".process" / "project.json").write_text(
                '{"project":"sample-project"}\n', encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", ".process/project.json"], cwd=root, check=True
            )
            subprocess.run(
                ["git", "commit", "-qm", "add project manifest"],
                cwd=root,
                check=True,
            )
            contract_path = inputs / "contract.json"
            plan_path = inputs / "plan.json"
            self.write_contract(contract_path)
            state = start_change(
                root,
                self.project(),
                contract_path,
                actor_id="producer",
                context_id="producer-context",
                kind="agent",
            )
            self.write_plan(plan_path, state["contract"]["digest"])
            register_plan(
                root,
                self.project(),
                "change-1",
                plan_path,
                actor_id="producer",
                context_id="producer-context",
                kind="agent",
            )
            signal = json.loads(
                (
                    Path(__file__).resolve().parent.parent
                    / "examples"
                    / "improvement-signal.json"
                ).read_text(encoding="utf-8")
            )
            signal["target"] = {
                "project": "sample-project",
                "repository": "example/producer",
            }
            signal_path = inputs / "signal.json"
            signal_path.write_text(json.dumps(signal) + "\n", encoding="utf-8")
            catalog = json.loads(
                (
                    Path(__file__).resolve().parent.parent
                    / "examples"
                    / "improvement-catalog.json"
                ).read_text(encoding="utf-8")
            )
            catalog["producer"] = signal["target"]
            catalog_path = inputs / "catalog.json"
            catalog_path.write_text(json.dumps(catalog) + "\n", encoding="utf-8")
            disposition_path = inputs / "disposition.json"
            create_improvement_disposition(
                root,
                signal_path,
                catalog_path,
                producer_repository="example/producer",
                decision="accepted",
                owner_boundary="shared-process",
                reusable_class="deterministic-enforcement",
                invariant_id="single-windows-helper-protocol",
                linked_change_id="change-1",
                rationale_sha256=f"sha256:{'e' * 64}",
                exception_approved_by=None,
                exception_evidence_sha256=None,
                output=disposition_path,
            )
            result = ingest_improvement_signal(
                root,
                "change-1",
                signal_path=signal_path,
                disposition_path=disposition_path,
                catalog_path=catalog_path,
                actor_id="producer",
                context_id="producer-context",
                actor_kind="agent",
            )

            self.assertEqual("producer-change", result["casePhase"])
            state = load_state(root, "change-1")
            self.assertEqual("producer", state["improvements"][0]["role"])
            self.assertEqual(
                "single-windows-helper-protocol",
                state["improvements"][0]["classification"]["invariantId"],
            )
            status_case = lifecycle_status(root, "change-1")["improvementStatus"][
                "cases"
            ][0]
            self.assertEqual("producer", status_case["role"])
            self.assertEqual("producer-change", status_case["phase"])
            self.assertEqual("recurrence", status_case["recurrence"])
            self.assertEqual("resolved", status_case["catalog"]["status"])
            self.assertEqual("change-1", status_case["producer"]["linkedChangeId"])
            with self.assertRaisesRegex(
                ContractError, "reviewed improvement-catalog.json"
            ):
                lifecycle_module._require_producer_catalog_activation(
                    root, state, state["improvements"][0]
                )
            catalog["entries"][0]["status"] = "active"
            catalog["entries"][0]["activeChangeId"] = "change-1"
            (root / "improvement-catalog.json").write_text(
                json.dumps(catalog) + "\n", encoding="utf-8"
            )
            lifecycle_module._require_producer_catalog_activation(
                root, state, state["improvements"][0]
            )
            catalog["entries"][0]["activeChangeId"] = "different-change"
            (root / "improvement-catalog.json").write_text(
                json.dumps(catalog) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ContractError, "selected lifecycle change"):
                lifecycle_module._require_producer_catalog_activation(
                    root, state, state["improvements"][0]
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

    @unittest.skipUnless(
        sys.platform == "win32", "Windows lifecycle repetition evidence"
    )
    def test_requested_changes_transition_repeats_on_fresh_windows_repositories(self):
        for iteration in range(8):
            with self.subTest(iteration=iteration):
                self.test_requested_changes_start_a_new_cycle_and_invalidate_evidence()

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
            self.assertEqual(state["phase"], "improvement-required")
            case = state["improvements"][0]
            with self.assertRaisesRegex(
                ContractError, "before improvement classification"
            ):
                begin_implementation(
                    root,
                    "change-1",
                    actor_id="implementer",
                    context_id="fix-context",
                    kind="agent",
                )
            classify_improvement_case(
                root,
                "change-1",
                case["id"],
                owner_boundary="project-local",
                reusable_class="local-behavior",
                invariant_id="reviewed-behavior-complete",
                disposition="local-fix",
                rationale_sha256=f"sha256:{'b' * 64}",
                target_project=None,
                target_repository=None,
                actor_id="implementer",
                context_id="fix-context",
                kind="agent",
            )
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
            state = load_state(root, "change-1")
            classify_improvement_case(
                root,
                "change-1",
                state["improvements"][0]["id"],
                owner_boundary="project-local",
                reusable_class="local-behavior",
                invariant_id="legacy-finding-preservation",
                disposition="local-fix",
                rationale_sha256=f"sha256:{'c' * 64}",
                target_project=None,
                target_repository=None,
                actor_id="implementer",
                context_id="fix-context",
                kind="agent",
            )
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
            del legacy["improvements"]
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
