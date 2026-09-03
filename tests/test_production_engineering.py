from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import unittest

from engineering_process.contracts import ProcessError, validate_document
from engineering_process.distribution import schemas_root
from engineering_process.production_engineering import (
    load_invariant_floor,
    validate_plan_assessments,
    validate_review_assessments,
)


ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = schemas_root(ROOT)


class ProductionEngineeringTests(unittest.TestCase):
    def setUp(self) -> None:
        floor = load_invariant_floor(ROOT)
        self.ids = [item["id"] for item in floor["invariants"]]
        self.plan = {
            "schemaVersion": 5,
            "changeId": "sample-change",
            "contractDigest": "sha256:" + "0" * 64,
            "approach": "Implement the accepted behavior.",
            "workItems": [
                {
                    "id": "implementation",
                    "outcome": "Implement and verify the change.",
                    "affectedPaths": ["src/"],
                }
            ],
            "risks": [],
            "productionEngineering": [
                {
                    "id": invariant_id,
                    "applicability": "applicable",
                    "rationale": "The change exercises this production boundary.",
                    "evidenceWorkItems": ["implementation"],
                }
                for invariant_id in self.ids
            ],
        }
        self.review = {
            "schemaVersion": 7,
            "changeId": "sample-change",
            "reviewer": {
                "actorId": "reviewer",
                "contextId": "independent-context",
                "kind": "agent",
            },
            "checkpoint": {
                "head": "0" * 40,
                "fingerprint": "sha256:" + "0" * 64,
                "fileCount": 1,
                "byteCount": 1,
            },
            "verdict": "approved",
            "summary": "The exact candidate satisfies the accepted behavior.",
            "findings": [],
            "productionEngineering": [
                {
                    "id": invariant_id,
                    "status": "satisfied",
                    "rationale": "The reviewed structure satisfies the invariant.",
                    "evidence": ["development and review profiles on the assigned snapshot"],
                }
                for invariant_id in self.ids
            ],
        }

    def test_floor_is_small_versioned_and_cross_domain(self) -> None:
        self.assertEqual(
            [
                "authoritative-structure",
                "single-policy-source",
                "bounded-side-effects",
                "contractual-evolution",
                "evidence-bound-assurance",
            ],
            self.ids,
        )

    def test_plan_requires_canonical_order_and_real_work_items(self) -> None:
        validate_document(self.plan, "plan", schema_root=SCHEMAS)
        validate_plan_assessments(self.plan, ROOT)

        for mutation in ("missing", "duplicate", "unknown", "reordered"):
            invalid = deepcopy(self.plan)
            if mutation == "missing":
                invalid["productionEngineering"].pop()
            elif mutation == "duplicate":
                invalid["productionEngineering"][-1]["id"] = self.ids[0]
            elif mutation == "unknown":
                invalid["productionEngineering"][-1]["id"] = "invented-invariant"
            else:
                invalid["productionEngineering"].reverse()
            with self.subTest(mutation=mutation), self.assertRaisesRegex(
                ProcessError, "canonical invariants"
            ):
                validate_plan_assessments(invalid, ROOT)

        dangling = deepcopy(self.plan)
        dangling["productionEngineering"][0]["evidenceWorkItems"] = ["missing"]
        with self.assertRaisesRegex(ProcessError, "unknown work items"):
            validate_plan_assessments(dangling, ROOT)

    def test_plan_version_four_remains_readable_without_new_assessments(self) -> None:
        legacy = deepcopy(self.plan)
        legacy["schemaVersion"] = 4
        legacy.pop("productionEngineering")
        validate_document(legacy, "plan", schema_root=SCHEMAS)
        validate_plan_assessments(legacy, ROOT)

        legacy["productionEngineering"] = self.plan["productionEngineering"]
        with self.assertRaisesRegex(ProcessError, "should not be valid"):
            validate_document(legacy, "plan", schema_root=SCHEMAS)

    def test_plan_applicability_shape_fails_closed(self) -> None:
        applicable_without_evidence = deepcopy(self.plan)
        applicable_without_evidence["productionEngineering"][0]["evidenceWorkItems"] = []
        with self.assertRaisesRegex(ProcessError, "non-empty"):
            validate_document(applicable_without_evidence, "plan", schema_root=SCHEMAS)

        not_applicable_with_evidence = deepcopy(self.plan)
        assessment = not_applicable_with_evidence["productionEngineering"][0]
        assessment["applicability"] = "not-applicable"
        with self.assertRaisesRegex(ProcessError, "expected to be empty"):
            validate_document(not_applicable_with_evidence, "plan", schema_root=SCHEMAS)

    def test_review_requires_canonical_independent_resolution(self) -> None:
        validate_document(self.review, "review", schema_root=SCHEMAS)
        validate_review_assessments(self.review, ROOT)

        missing = deepcopy(self.review)
        missing["productionEngineering"].pop()
        with self.assertRaisesRegex(ProcessError, "canonical invariants"):
            validate_review_assessments(missing, ROOT)

        violation = deepcopy(self.review)
        violation["verdict"] = "changes-requested"
        violation["findings"] = [
            {
                "id": "invariant-failure",
                "severity": "blocking",
                "priority": "P1",
                "criterionId": "works",
                "origin": "production-invariant",
                "summary": "The implementation guesses open-world meaning.",
                "location": "src/decision.py",
            }
        ]
        assessment = violation["productionEngineering"][0]
        assessment.update(
            status="violated",
            rationale="The decision uses a lexical guess instead of authoritative state.",
            evidence=[],
            findingId="invariant-failure",
        )
        validate_document(violation, "review", schema_root=SCHEMAS)
        validate_review_assessments(violation, ROOT)

        violation["findings"] = []
        with self.assertRaisesRegex(ProcessError, "blocking finding"):
            validate_review_assessments(violation, ROOT)

    def test_review_cannot_approve_a_violation_or_leave_invariant_finding_unlinked(self) -> None:
        violation = deepcopy(self.review)
        assessment = violation["productionEngineering"][0]
        assessment.update(
            status="violated",
            rationale="The invariant remains violated.",
            evidence=[],
            findingId="invariant-failure",
        )
        with self.assertRaises(ProcessError):
            validate_document(violation, "review", schema_root=SCHEMAS)

        unlinked = deepcopy(self.review)
        unlinked["findings"] = [
            {
                "id": "unlinked",
                "severity": "blocking",
                "priority": "P1",
                "criterionId": "works",
                "origin": "production-invariant",
                "summary": "This invariant finding has no assessment link.",
            }
        ]
        with self.assertRaisesRegex(ProcessError, "must be linked"):
            validate_review_assessments(unlinked, ROOT)


if __name__ == "__main__":
    unittest.main()
