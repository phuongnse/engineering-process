import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import jsonschema

from engineering_process.cli import main
from engineering_process.contracts import (
    ContractError,
    canonical_json_digest,
    validate_recommendation,
    validate_recommendation_resolution,
    validate_recommendation_review,
    validate_recommendation_review_assignment,
)
from engineering_process.recommendation import (
    create_recommendation_resolution,
    start_recommendation_review,
    validate_recommendation_chain,
)
from engineering_process.lifecycle import reserve_review_context


PROCESS_ROOT = Path(__file__).resolve().parent.parent


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.recommendation = self.load_example("recommendation")
        self.review = self.load_example("recommendation-review")
        self.resolution = self.load_example("recommendation-resolution")

    def load_example(self, name: str):
        return json.loads(
            (PROCESS_ROOT / "examples" / f"{name}.json").read_text(
                encoding="utf-8"
            )
        )

    def write(self, root: Path, name: str, document) -> Path:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    def start_assignment(
        self,
        root: Path,
        recommendation_path: Path,
        *,
        actor_id: str = "independent-decision-reviewer",
        context_id: str = "fresh-decision-review-context",
        kind: str = "agent",
        method: str = "isolated-context",
        attested_by: str = "agent-host",
    ) -> Path:
        result = start_recommendation_review(
            root,
            recommendation_path,
            actor_id=actor_id,
            context_id=context_id,
            kind=kind,
            method=method,
            attested_by=attested_by,
            evidence="The host created a fresh context outside the coordinator.",
        )
        return root / result["assignment"]

    def prepare_chain(self, root: Path):
        recommendation_path = self.write(
            root, "recommendation.json", self.recommendation
        )
        assignment_path = self.start_assignment(root, recommendation_path)
        assignment = json.loads(assignment_path.read_text(encoding="utf-8"))
        self.review["decisionId"] = self.recommendation["decisionId"]
        self.review["recommendationSha256"] = canonical_json_digest(
            self.recommendation
        )
        self.review["assignmentSha256"] = canonical_json_digest(assignment)
        self.review["reviewer"] = assignment["reviewer"]
        review_path = self.write(root, "review.json", self.review)
        return recommendation_path, assignment_path, review_path

    def prepare_resolution(self, root: Path):
        recommendation_path, assignment_path, review_path = self.prepare_chain(root)
        assignment = json.loads(assignment_path.read_text(encoding="utf-8"))
        self.resolution["decisionId"] = self.recommendation["decisionId"]
        self.resolution["recommendationSha256"] = canonical_json_digest(
            self.recommendation
        )
        self.resolution["assignmentSha256"] = canonical_json_digest(assignment)
        self.resolution["reviewSha256"] = canonical_json_digest(self.review)
        resolution_path = self.write(root, "resolution.json", self.resolution)
        return recommendation_path, assignment_path, review_path, resolution_path

    def test_incident_option_cannot_be_declared_valid_or_recommended(self):
        invalid = self.recommendation["options"][0]
        invalid["classification"] = "valid"
        self.recommendation["validOptionIds"] = [
            "complete-before-remote-evidence",
            "verify-before-completion",
        ]
        self.recommendation["recommendation"]["optionId"] = (
            "complete-before-remote-evidence"
        )

        with self.assertRaisesRegex(ContractError, "must be derived as invalid"):
            validate_recommendation(self.recommendation)

    def test_unproven_assumption_cannot_leave_option_valid(self):
        assumption = self.recommendation["assumptions"][0]
        assumption["status"] = "unproven"
        assumption["evidenceSha256"] = None

        with self.assertRaisesRegex(ContractError, "must be derived as unproven"):
            validate_recommendation(self.recommendation)

    def test_each_option_must_assess_every_invariant_exactly_once(self):
        self.recommendation["options"][1]["invariantAssessments"].pop()

        with self.assertRaisesRegex(ContractError, "cover every invariant"):
            validate_recommendation(self.recommendation)

    def test_schema_and_semantic_tradeoff_bounds_match(self):
        self.recommendation["options"][0]["tradeoffs"] = ["x" * 1001]
        schema = json.loads(
            (PROCESS_ROOT / "schemas" / "recommendation.schema.json").read_text(
                encoding="utf-8"
            )
        )

        with self.assertRaisesRegex(ContractError, "exceeds 1000"):
            validate_recommendation(self.recommendation)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(self.recommendation)

    def test_schema_and_semantic_integer_types_reject_booleans(self):
        first_option = self.recommendation["options"][0]
        first_option["invariantAssessments"][0]["status"] = "satisfied"
        first_option["classification"] = "valid"
        self.recommendation["validOptionIds"] = [
            "complete-before-remote-evidence",
            "verify-before-completion",
        ]
        self.recommendation["recommendation"]["optimizationCriteria"] = [
            {
                "id": "minimum-workflow-cost",
                "priority": True,
                "rationaleSha256": f"sha256:{'f' * 64}",
            }
        ]
        schema = json.loads(
            (PROCESS_ROOT / "schemas" / "recommendation.schema.json").read_text(
                encoding="utf-8"
            )
        )
        with self.assertRaisesRegex(ContractError, "sequence starting at 1"):
            validate_recommendation(self.recommendation)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(self.recommendation)

        for document, validator in (
            (self.load_example("recommendation"), validate_recommendation),
            (
                self.load_example("recommendation-review-assignment"),
                validate_recommendation_review_assignment,
            ),
            (self.load_example("recommendation-review"), validate_recommendation_review),
            (
                self.load_example("recommendation-resolution"),
                validate_recommendation_resolution,
            ),
        ):
            with self.subTest(kind=document["kind"]):
                document["schemaVersion"] = True
                with self.assertRaisesRegex(ContractError, "integer 1"):
                    validator(document)

    def test_review_start_rejects_method_mismatch_and_participant_attester(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recommendation_path = self.write(
                root, "recommendation.json", self.recommendation
            )
            with self.assertRaisesRegex(ContractError, "isolated-context"):
                self.start_assignment(
                    root,
                    recommendation_path,
                    method="separate-person",
                )
            with self.assertRaisesRegex(ContractError, "participant-attested"):
                self.start_assignment(
                    root,
                    recommendation_path,
                    attested_by="independent-decision-reviewer",
                )

    def test_cli_review_start_reserves_context_and_emits_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recommendation_path = self.write(
                root, "recommendation.json", self.recommendation
            )
            stdout = io.StringIO()
            with patch(
                "engineering_process.cli._lifecycle_project"
            ), contextlib.redirect_stdout(stdout):
                result = main(
                    [
                        "recommendation",
                        "review",
                        "start",
                        "--project-root",
                        str(root),
                        "--recommendation",
                        str(recommendation_path),
                        "--actor",
                        "independent-decision-reviewer",
                        "--context",
                        "fresh-cli-review-context",
                        "--actor-kind",
                        "agent",
                        "--method",
                        "isolated-context",
                        "--attested-by",
                        "agent-host",
                        "--attestation-evidence",
                        "The host created a fresh isolated context.",
                        "--json",
                    ]
                )

            self.assertEqual(0, result)
            report = json.loads(stdout.getvalue())
            self.assertEqual("review-pending", report["phase"])
            self.assertTrue((root / report["assignment"]).is_file())

    def test_reviewer_context_cannot_be_reused_across_recommendations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.write(root, "first.json", self.recommendation)
            self.start_assignment(root, first)
            second_document = copy.deepcopy(self.recommendation)
            second_document["decisionId"] = "second-remote-evidence-ordering"
            second = self.write(root, "second.json", second_document)

            with self.assertRaisesRegex(ContractError, "any review assignment"):
                self.start_assignment(root, second)

    def test_reviewer_context_cannot_reuse_lifecycle_review_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reserve_review_context(
                root,
                {"changeId": "existing-lifecycle-change", "cycle": 1},
                {
                    "actorId": "lifecycle-reviewer",
                    "contextId": "shared-project-review-context",
                    "kind": "agent",
                },
            )
            recommendation_path = self.write(
                root, "recommendation.json", self.recommendation
            )

            with self.assertRaisesRegex(ContractError, "any review assignment"):
                self.start_assignment(
                    root,
                    recommendation_path,
                    actor_id="recommendation-reviewer",
                    context_id="shared-project-review-context",
                )

    def test_no_valid_option_is_blocked_without_optimization(self):
        valid = self.recommendation["options"][1]
        valid["invariantAssessments"][0]["status"] = "violated"
        valid["classification"] = "invalid"
        self.recommendation["validOptionIds"] = []
        self.recommendation["recommendation"] = {
            "status": "blocked",
            "optionId": None,
            "rationaleSha256": f"sha256:{'b' * 64}",
            "optimizationCriteria": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recommendation_path, assignment_path, review_path = self.prepare_chain(root)
            result = validate_recommendation_chain(
                root, recommendation_path, assignment_path, review_path
            )

        self.assertFalse(result["allowed"])
        self.assertEqual("blocked", result["phase"])
        self.assertEqual([], result["validOptionIds"])

    def test_chain_rejects_stale_digest_and_assignment_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recommendation_path, assignment_path, _review_path = self.prepare_chain(root)
            self.review["recommendationSha256"] = f"sha256:{'f' * 64}"
            stale_review = self.write(root, "stale-review.json", self.review)
            with self.assertRaisesRegex(ContractError, "canonical recommendation"):
                validate_recommendation_chain(
                    root, recommendation_path, assignment_path, stale_review
                )

            self.review["recommendationSha256"] = canonical_json_digest(
                self.recommendation
            )
            self.review["assignmentSha256"] = f"sha256:{'e' * 64}"
            stale_assignment = self.write(
                root, "stale-assignment-review.json", self.review
            )
            with self.assertRaisesRegex(ContractError, "canonical assignment"):
                validate_recommendation_chain(
                    root, recommendation_path, assignment_path, stale_assignment
                )

            forged = json.loads(assignment_path.read_text(encoding="utf-8"))
            forged["contextReservationSha256"] = f"sha256:{'a' * 64}"
            forged_path = self.write(root, "forged-assignment.json", forged)
            self.review["assignmentSha256"] = canonical_json_digest(forged)
            forged_review = self.write(root, "forged-review.json", self.review)
            with self.assertRaisesRegex(ContractError, "context reservation"):
                validate_recommendation_chain(
                    root, recommendation_path, forged_path, forged_review
                )

    def test_chain_requires_complete_option_challenge_and_approved_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recommendation_path, assignment_path, _review_path = self.prepare_chain(root)
            self.review["optionAssessments"].pop()
            incomplete = self.write(root, "incomplete-review.json", self.review)
            with self.assertRaisesRegex(ContractError, "assess every option"):
                validate_recommendation_chain(
                    root, recommendation_path, assignment_path, incomplete
                )

            self.review = self.load_example("recommendation-review")
            assignment = json.loads(assignment_path.read_text(encoding="utf-8"))
            self.review["recommendationSha256"] = canonical_json_digest(
                self.recommendation
            )
            self.review["assignmentSha256"] = canonical_json_digest(assignment)
            self.review["reviewer"] = assignment["reviewer"]
            self.review["challengeAssessments"][3]["status"] = "failed"
            self.review["verdict"] = "changes-requested"
            self.review["findings"] = [
                {
                    "id": "terminal-ordering",
                    "severity": "high",
                    "summary": "Required evidence follows completion",
                    "evidence": "The proposed transition reaches completion first.",
                    "status": "open",
                    "resolutionEvidence": None,
                }
            ]
            rejected = self.write(root, "rejected-review.json", self.review)
            with self.assertRaisesRegex(ContractError, "approved independent"):
                validate_recommendation_chain(
                    root, recommendation_path, assignment_path, rejected
                )

    def test_resolution_cannot_select_invalid_option_or_grant_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.prepare_resolution(root)
            self.resolution["selectedOptionId"] = "complete-before-remote-evidence"
            invalid = self.write(root, "invalid-resolution.json", self.resolution)
            with self.assertRaisesRegex(ContractError, "derived valid option"):
                validate_recommendation_chain(root, *paths[:3], invalid)

        self.resolution["controls"]["grantsMerge"] = True
        with self.assertRaisesRegex(ContractError, "grantsMerge.*false"):
            validate_recommendation_resolution(self.resolution)

    def test_resolution_output_rejects_dangling_symlink(self):
        if os.name != "posix":
            self.skipTest("POSIX symlink contract")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recommendation_path, assignment_path, review_path = self.prepare_chain(root)
            redirected = root / "redirected.json"
            output = root / "resolution.json"
            output.symlink_to(redirected)

            with self.assertRaisesRegex(ContractError, "refusing to replace"):
                create_recommendation_resolution(
                    root,
                    recommendation_path,
                    assignment_path,
                    review_path,
                    selected_option_id="verify-before-completion",
                    owner_id="project-owner",
                    owner_evidence_sha256=f"sha256:{'c' * 64}",
                    selection_rationale_sha256=f"sha256:{'d' * 64}",
                    output=output,
                )

            self.assertFalse(redirected.exists())

    def test_resolution_output_never_overwrites_concurrent_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recommendation_path, assignment_path, review_path = self.prepare_chain(root)
            output = root / "resolution.json"
            real_open = os.open

            def create_concurrent(path, flags, mode=0o777, *, dir_fd=None):
                if Path(path) == output:
                    output.write_text("concurrent\n", encoding="utf-8")
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with patch(
                "engineering_process.recommendation.os.open",
                side_effect=create_concurrent,
            ), self.assertRaisesRegex(ContractError, "refusing to replace"):
                create_recommendation_resolution(
                    root,
                    recommendation_path,
                    assignment_path,
                    review_path,
                    selected_option_id="verify-before-completion",
                    owner_id="project-owner",
                    owner_evidence_sha256=f"sha256:{'c' * 64}",
                    selection_rationale_sha256=f"sha256:{'d' * 64}",
                    output=output,
                )

            self.assertEqual("concurrent\n", output.read_text(encoding="utf-8"))

    def test_resolution_output_detects_parent_identity_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recommendation_path, assignment_path, review_path = self.prepare_chain(root)
            parent = root / "artifacts"
            displaced = root / "artifacts-displaced"
            parent.mkdir()
            output = parent / "resolution.json"
            real_open = os.open
            swapped = False

            def swap_parent(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if Path(path) == output and not swapped:
                    parent.rename(displaced)
                    parent.mkdir()
                    swapped = True
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with patch(
                "engineering_process.recommendation.os.open",
                side_effect=swap_parent,
            ), self.assertRaisesRegex(ContractError, "parent identity changed"):
                create_recommendation_resolution(
                    root,
                    recommendation_path,
                    assignment_path,
                    review_path,
                    selected_option_id="verify-before-completion",
                    owner_id="project-owner",
                    owner_evidence_sha256=f"sha256:{'c' * 64}",
                    selection_rationale_sha256=f"sha256:{'d' * 64}",
                    output=output,
                )

            self.assertFalse(output.exists())
            self.assertFalse((displaced / "resolution.json").exists())

    def test_cli_creates_exact_resolution_and_refuses_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recommendation_path, assignment_path, review_path = self.prepare_chain(root)
            output = root / "created-resolution.json"
            validate_stdout = io.StringIO()
            with patch(
                "engineering_process.cli._lifecycle_project"
            ), contextlib.redirect_stdout(validate_stdout):
                validated = main(
                    [
                        "recommendation",
                        "validate-chain",
                        "--project-root",
                        str(root),
                        "--recommendation",
                        str(recommendation_path),
                        "--assignment",
                        str(assignment_path),
                        "--review",
                        str(review_path),
                        "--json",
                    ]
                )
            self.assertEqual(0, validated)
            self.assertEqual(
                "recommendation-approved",
                json.loads(validate_stdout.getvalue())["phase"],
            )
            arguments = [
                "recommendation",
                "resolution",
                "--project-root",
                str(root),
                "--recommendation",
                str(recommendation_path),
                "--assignment",
                str(assignment_path),
                "--review",
                str(review_path),
                "--selected-option",
                "verify-before-completion",
                "--owner-id",
                "project-owner",
                "--owner-evidence-sha256",
                f"sha256:{'c' * 64}",
                "--selection-rationale-sha256",
                f"sha256:{'d' * 64}",
                "--output",
                str(output),
                "--json",
            ]
            stdout = io.StringIO()
            with patch(
                "engineering_process.cli._lifecycle_project"
            ), contextlib.redirect_stdout(stdout):
                result = main(arguments)

            self.assertEqual(0, result)
            created = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("verify-before-completion", created["selectedOptionId"])
            self.assertTrue(all(value is False for value in created["controls"].values()))
            self.assertEqual("resolved", json.loads(stdout.getvalue())["phase"])

            repeated_stdout = io.StringIO()
            with patch(
                "engineering_process.cli._lifecycle_project"
            ), contextlib.redirect_stdout(repeated_stdout):
                repeated = main(arguments)
            self.assertEqual(2, repeated)
            self.assertIn(
                "refusing to replace",
                "\n".join(json.loads(repeated_stdout.getvalue())["errors"]),
            )

    @unittest.skipUnless(os.name == "posix", "POSIX symlink contract")
    def test_chain_rejects_symlink_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recommendation_path, assignment_path, review_path = self.prepare_chain(root)
            link = root / "recommendation-link.json"
            link.symlink_to(recommendation_path)
            with self.assertRaisesRegex(ContractError, "regular non-symlink"):
                validate_recommendation_chain(
                    root, link, assignment_path, review_path
                )


if __name__ == "__main__":
    unittest.main()
