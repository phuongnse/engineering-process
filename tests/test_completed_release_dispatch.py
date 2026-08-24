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


if __name__ == "__main__":
    unittest.main()
