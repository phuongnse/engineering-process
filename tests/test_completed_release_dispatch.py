import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from engineering_process.contracts import ContractError
from verification.dispatch_completed_release import (
    dispatch_completed_release,
    publication_dispatch_request,
)
from verification.reconcile_completed_release import reconcile_completed_release


class Response:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return b""


class CompletedReleaseDispatchTests(unittest.TestCase):
    def test_protected_policy_validator_accepts_only_complete_standing_authority(self):
        root = Path(__file__).resolve().parent.parent
        validator = root / "verification" / "validate_protected_automation_policy.py"
        policy = json.loads(
            (root / ".process" / "automation.json").read_text(encoding="utf-8")
        )

        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "policy.json"

            def validate(document):
                policy_path.write_text(
                    json.dumps(document, sort_keys=True), encoding="utf-8"
                )
                return subprocess.run(
                    [sys.executable, str(validator), "--policy", str(policy_path)],
                    check=False,
                    capture_output=True,
                    text=True,
                )

            valid = validate(policy)
            self.assertEqual(0, valid.returncode, valid.stderr)
            self.assertEqual(
                {"mergeMethod": "squash", "status": "valid"},
                json.loads(valid.stdout),
            )

            invalid_policies = []
            for field, value in (
                ("enabled", False),
                ("kind", "other-policy"),
                ("confirmationMode", "always"),
                ("actions", policy["actions"][:-1]),
                ("escalationReasons", policy["escalationReasons"][:-1]),
            ):
                invalid = json.loads(json.dumps(policy))
                invalid[field] = value
                invalid_policies.append((field, invalid))
            missing_gate = json.loads(json.dumps(policy))
            del missing_gate["merge"]["requireRequiredChecks"]
            invalid_policies.append(("missing-merge-gate", missing_gate))
            disabled_gate = json.loads(json.dumps(policy))
            disabled_gate["merge"]["requireIndependentReview"] = False
            invalid_policies.append(("disabled-merge-gate", disabled_gate))

            for label, invalid in invalid_policies:
                with self.subTest(label=label):
                    rejected = validate(invalid)
                    self.assertNotEqual(0, rejected.returncode)
                    self.assertIn("protected automation policy", rejected.stderr)

    def test_builds_one_bounded_secret_free_dispatch(self):
        encoded = base64.b64encode(b"gzip evidence")
        request = publication_dispatch_request(
            verified_run_id="123",
            comparison_base="a" * 40,
            release_head_sha="b" * 40,
            encoded_evidence=encoded,
            token="token-value",
        )
        body = json.loads(request.data)

        self.assertEqual("POST", request.method)
        self.assertEqual("main", body["ref"])
        self.assertEqual("123", body["inputs"]["verified_run_id"])
        self.assertEqual(encoded.decode("ascii"), body["inputs"]["completion_evidence_gzip_base64"])
        self.assertNotIn("token-value", request.data.decode("utf-8"))

    def test_dispatch_requires_a_stable_regular_file_and_exact_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence.txt"
            evidence.write_bytes(base64.b64encode(b"gzip evidence"))
            calls = []

            def opener(request, *, timeout):
                calls.append((request, timeout))
                return Response()

            dispatch_completed_release(
                verified_run_id="123",
                comparison_base="a" * 40,
                release_head_sha="b" * 40,
                completion_evidence=evidence,
                token="token-value",
                opener=opener,
            )
            self.assertEqual(30, calls[0][1])

            link = root / "link.txt"
            link.symlink_to(evidence)
            with self.assertRaisesRegex(ContractError, "stable regular file"):
                dispatch_completed_release(
                    verified_run_id="123",
                    comparison_base="a" * 40,
                    release_head_sha="b" * 40,
                    completion_evidence=link,
                    token="token-value",
                    opener=opener,
                )

    def test_duplicate_callback_converges_on_the_exact_ready_release_pr(self):
        body = "approved release body\n"
        expected_head = "b" * 40
        existing = {
            "baseRefName": "main",
            "body": body,
            "headRefName": "automation/release/next",
            "headRefOid": expected_head,
            "isDraft": False,
            "mergedAt": None,
            "number": 42,
            "state": "OPEN",
            "title": "chore(release): prepare v0.5.0",
        }

        self.assertEqual(
            {"action": "publish-and-create", "pullRequestNumber": None},
            reconcile_completed_release(
                open_pull_requests=[],
                remote_head_sha="",
                expected_head_sha=expected_head,
                expected_base="main",
                expected_branch="automation/release/next",
                expected_title=existing["title"],
                expected_body=body,
            ),
        )
        self.assertEqual(
            {"action": "existing", "pullRequestNumber": 42},
            reconcile_completed_release(
                open_pull_requests=[existing],
                remote_head_sha=expected_head,
                expected_head_sha=expected_head,
                expected_base="main",
                expected_branch="automation/release/next",
                expected_title=existing["title"],
                expected_body=body,
            ),
        )

        merged = {
            **existing,
            "mergedAt": "2026-08-24T13:00:00Z",
            "state": "MERGED",
        }
        self.assertEqual(
            {"action": "merged", "pullRequestNumber": 42},
            reconcile_completed_release(
                open_pull_requests=[merged],
                remote_head_sha="",
                expected_head_sha=expected_head,
                expected_base="main",
                expected_branch="automation/release/next",
                expected_title=existing["title"],
                expected_body=body,
            ),
        )

        historical = {
            **merged,
            "body": "older release\n",
            "headRefOid": "c" * 40,
            "title": "chore(release): prepare v0.4.0",
        }
        self.assertEqual(
            {"action": "publish-and-create", "pullRequestNumber": None},
            reconcile_completed_release(
                open_pull_requests=[historical],
                remote_head_sha="",
                expected_head_sha=expected_head,
                expected_base="main",
                expected_branch="automation/release/next",
                expected_title=existing["title"],
                expected_body=body,
            ),
        )

        self.assertEqual(
            {"action": "existing", "pullRequestNumber": 42},
            reconcile_completed_release(
                open_pull_requests=[historical, existing],
                remote_head_sha=expected_head,
                expected_head_sha=expected_head,
                expected_base="main",
                expected_branch="automation/release/next",
                expected_title=existing["title"],
                expected_body=body,
            ),
        )

    def test_duplicate_callback_fails_closed_on_existing_publication_mismatch(self):
        expected_head = "b" * 40
        existing = {
            "baseRefName": "main",
            "body": "different evidence digest\n",
            "headRefName": "automation/release/next",
            "headRefOid": expected_head,
            "isDraft": False,
            "mergedAt": None,
            "number": 42,
            "state": "OPEN",
            "title": "chore(release): prepare v0.5.0",
        }
        with self.assertRaisesRegex(ContractError, "body"):
            reconcile_completed_release(
                open_pull_requests=[existing],
                remote_head_sha=expected_head,
                expected_head_sha=expected_head,
                expected_base="main",
                expected_branch="automation/release/next",
                expected_title=existing["title"],
                expected_body="approved release body\n",
            )


if __name__ == "__main__":
    unittest.main()
