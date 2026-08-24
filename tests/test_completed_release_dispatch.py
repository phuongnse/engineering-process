import base64
import json
from pathlib import Path
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
            "number": 42,
            "title": "chore(release): prepare v0.5.0",
        }

        self.assertEqual(
            "publish-and-create",
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
            "existing",
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

    def test_duplicate_callback_fails_closed_on_existing_publication_mismatch(self):
        expected_head = "b" * 40
        existing = {
            "baseRefName": "main",
            "body": "different evidence digest\n",
            "headRefName": "automation/release/next",
            "headRefOid": expected_head,
            "isDraft": False,
            "number": 42,
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
