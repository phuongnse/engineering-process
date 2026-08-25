import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from engineering_process.cli import main
from engineering_process.contracts import (
    ContractError,
    canonical_json_digest,
    validate_recommendation,
    validate_recommendation_resolution,
)
from engineering_process.recommendation import validate_recommendation_chain


PROCESS_ROOT = Path(__file__).resolve().parent.parent


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.recommendation = json.loads(
            (PROCESS_ROOT / "examples" / "recommendation.json").read_text(
                encoding="utf-8"
            )
        )
        self.review = json.loads(
            (PROCESS_ROOT / "examples" / "recommendation-review.json").read_text(
                encoding="utf-8"
            )
        )
        self.resolution = json.loads(
            (
                PROCESS_ROOT / "examples" / "recommendation-resolution.json"
            ).read_text(encoding="utf-8")
        )

    def write(self, root: Path, name: str, document) -> Path:
        path = root / name
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    def bind_review(self):
        self.review["recommendationSha256"] = canonical_json_digest(
            self.recommendation
        )

    def bind_resolution(self):
        self.resolution["recommendationSha256"] = canonical_json_digest(
            self.recommendation
        )
        self.resolution["reviewSha256"] = canonical_json_digest(self.review)

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
        self.bind_review()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recommendation_path = self.write(
                root, "recommendation.json", self.recommendation
            )
            review_path = self.write(root, "review.json", self.review)
            result = validate_recommendation_chain(
                recommendation_path, review_path
            )

        self.assertFalse(result["allowed"])
        self.assertEqual("blocked", result["phase"])
        self.assertEqual([], result["validOptionIds"])

    def test_chain_rejects_stale_digest_and_self_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recommendation_path = self.write(
                root, "recommendation.json", self.recommendation
            )
            self.review["recommendationSha256"] = f"sha256:{'f' * 64}"
            review_path = self.write(root, "review.json", self.review)
            with self.assertRaisesRegex(ContractError, "canonical recommendation"):
                validate_recommendation_chain(recommendation_path, review_path)

            self.bind_review()
            self.review["reviewer"]["actorId"] = self.recommendation["coordinator"][
                "actorId"
            ]
            review_path = self.write(root, "self-review.json", self.review)
            with self.assertRaisesRegex(ContractError, "actor must differ"):
                validate_recommendation_chain(recommendation_path, review_path)

    def test_chain_requires_complete_option_and_invariant_challenge(self):
        self.review["optionAssessments"].pop()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recommendation_path = self.write(
                root, "recommendation.json", self.recommendation
            )
            review_path = self.write(root, "review.json", self.review)
            with self.assertRaisesRegex(ContractError, "assess every option"):
                validate_recommendation_chain(recommendation_path, review_path)

    def test_chain_rejects_changes_requested_challenge(self):
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
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recommendation_path = self.write(
                root, "recommendation.json", self.recommendation
            )
            review_path = self.write(root, "review.json", self.review)
            with self.assertRaisesRegex(ContractError, "approved independent"):
                validate_recommendation_chain(recommendation_path, review_path)

    def test_resolution_cannot_select_invalid_option_or_grant_authority(self):
        self.resolution["selectedOptionId"] = "complete-before-remote-evidence"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recommendation_path = self.write(
                root, "recommendation.json", self.recommendation
            )
            review_path = self.write(root, "review.json", self.review)
            resolution_path = self.write(root, "resolution.json", self.resolution)
            with self.assertRaisesRegex(ContractError, "derived valid option"):
                validate_recommendation_chain(
                    recommendation_path, review_path, resolution_path
                )

        self.resolution["controls"]["grantsMerge"] = True
        with self.assertRaisesRegex(ContractError, "grantsMerge.*false"):
            validate_recommendation_resolution(self.resolution)

    def test_cli_validates_chain_and_creates_exact_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recommendation_path = self.write(
                root, "recommendation.json", self.recommendation
            )
            review_path = self.write(root, "review.json", self.review)
            output = root / "created-resolution.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = main(
                    [
                        "recommendation",
                        "resolution",
                        "--recommendation",
                        str(recommendation_path),
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
                )

            self.assertEqual(0, result)
            created = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("verify-before-completion", created["selectedOptionId"])
            self.assertTrue(all(value is False for value in created["controls"].values()))
            self.assertEqual(
                "resolved", json.loads(stdout.getvalue())["phase"]
            )
            repeated_stdout = io.StringIO()
            with contextlib.redirect_stdout(repeated_stdout):
                repeated = main(
                    [
                        "recommendation",
                        "resolution",
                        "--recommendation",
                        str(recommendation_path),
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
                )
            self.assertEqual(2, repeated)
            self.assertIn(
                "refusing to replace",
                "\n".join(json.loads(repeated_stdout.getvalue())["errors"]),
            )

    @unittest.skipUnless(os.name == "posix", "POSIX symlink contract")
    def test_chain_rejects_symlink_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.write(root, "target.json", self.recommendation)
            link = root / "recommendation.json"
            link.symlink_to(target)
            review_path = self.write(root, "review.json", self.review)
            with self.assertRaisesRegex(ContractError, "regular non-symlink"):
                validate_recommendation_chain(link, review_path)


if __name__ == "__main__":
    unittest.main()
