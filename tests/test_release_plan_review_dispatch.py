from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from verification.render_release_plan_review_dispatch import (
    DispatchContractError,
    MAX_CLIENT_PAYLOAD_PROPERTIES,
    MAX_EVENT_BYTES,
    encode_event,
    render_event,
    write_event,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "verification" / "render_release_plan_review_dispatch.py"
BASE = "a" * 40
COMMIT = "b" * 40


def valid_arguments() -> dict[str, object]:
    return {
        "artifact": f"planned-release-candidate-{COMMIT}",
        "comparison_base": BASE,
        "change_id": "release-0-9-0",
        "commit": COMMIT,
        "continuation_workflow": "release-plan-approval.yml",
        "max_plan_decision_review_bytes": 60_000,
        "plan_decision_review_encoding": "gzip+base64",
        "repository": "phuongnse/engineering-process",
        "reviewer_actor": "independent-plan-reviewer",
        "reviewer_context": f"release-plan-review-{COMMIT}",
        "planned_run_id": "33153471577",
        "planned_run_attempt": 2,
    }


class ReleasePlanReviewDispatchTests(unittest.TestCase):
    def test_renders_exact_bounded_envelope_and_nested_run_identity(self):
        event = render_event(**valid_arguments())
        payload = event["client_payload"]

        self.assertEqual(
            MAX_CLIENT_PAYLOAD_PROPERTIES,
            len(payload),
        )
        self.assertEqual(
            {"id": "33153471577", "attempt": 2},
            payload["plannedRun"],
        )
        self.assertNotIn("plannedRunId", payload)
        self.assertNotIn("plannedRunAttempt", payload)
        self.assertEqual(BASE, payload["comparisonBase"])
        self.assertEqual(COMMIT, payload["commit"])
        self.assertLessEqual(len(encode_event(event)), MAX_EVENT_BYTES)

    def test_cli_writes_the_provider_artifact_exclusively(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "dispatch.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--artifact",
                    f"planned-release-candidate-{COMMIT}",
                    "--comparison-base",
                    BASE,
                    "--change-id",
                    "release-0-9-0",
                    "--commit",
                    COMMIT,
                    "--continuation-workflow",
                    "release-plan-approval.yml",
                    "--max-plan-decision-review-bytes",
                    "60000",
                    "--plan-decision-review-encoding",
                    "gzip+base64",
                    "--repository",
                    "phuongnse/engineering-process",
                    "--reviewer-actor",
                    "independent-plan-reviewer",
                    "--reviewer-context",
                    f"release-plan-review-{COMMIT}",
                    "--planned-run-id",
                    "33153471577",
                    "--planned-run-attempt",
                    "2",
                    "--output",
                    str(output),
                ],
                cwd=PROJECT_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("rendered release plan-review dispatch", completed.stdout)
            self.assertEqual(
                {"id": "33153471577", "attempt": 2},
                json.loads(output.read_text(encoding="utf-8"))["client_payload"][
                    "plannedRun"
                ],
            )
            with self.assertRaisesRegex(
                DispatchContractError, "cannot write repository dispatch"
            ):
                write_event(render_event(**valid_arguments()), output)

    def test_rejects_invalid_authority_and_provider_inputs(self):
        cases = (
            ("comparison_base", "A" * 40, "comparison base"),
            ("commit", BASE, "must differ"),
            ("artifact", "planned-release-candidate-wrong", "artifact"),
            ("change_id", "Release-0-9-0", "change id"),
            ("continuation_workflow", "other.yml", "continuation workflow"),
            ("max_plan_decision_review_bytes", 60_001, "exceeds"),
            ("plan_decision_review_encoding", "base64", "encoding"),
            ("repository", "not-a-repository", "repository"),
            ("reviewer_actor", "reviewer\nactor", "control character"),
            ("reviewer_context", "", "between 1"),
            ("planned_run_id", "0", "positive decimal"),
            ("planned_run_id", "12x", "positive decimal"),
            ("planned_run_attempt", 0, "positive integer"),
            ("planned_run_attempt", True, "positive integer"),
        )
        for field, value, message in cases:
            with self.subTest(field=field, value=value):
                arguments = valid_arguments()
                arguments[field] = value
                with self.assertRaisesRegex(DispatchContractError, message):
                    render_event(**arguments)

    def test_rejects_property_overflow_and_oversized_event(self):
        event = render_event(**valid_arguments())
        event["client_payload"]["unexpected"] = True
        with self.assertRaisesRegex(DispatchContractError, "property limit"):
            encode_event(event)

        event = render_event(**valid_arguments())
        event["client_payload"]["reviewer"]["contextId"] = "x" * MAX_EVENT_BYTES
        with self.assertRaisesRegex(DispatchContractError, "byte limit"):
            encode_event(event)

    def test_rejects_non_regular_output_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            regular_parent = root / "regular"
            regular_parent.mkdir()
            linked_parent = root / "linked"
            try:
                linked_parent.symlink_to(regular_parent, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlink unavailable: {error}")
            with self.assertRaisesRegex(
                DispatchContractError, "regular directory"
            ):
                write_event(
                    render_event(**valid_arguments()),
                    linked_parent / "dispatch.json",
                )


if __name__ == "__main__":
    unittest.main()
