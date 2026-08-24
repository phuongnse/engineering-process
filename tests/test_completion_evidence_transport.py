import base64
import gzip
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from engineering_process.contracts import ContractError
from engineering_process.evidence import MAX_RECEIPT_BYTES
from engineering_process.evidence_transport import (
    decode_completion_evidence,
    encode_completion_evidence,
)


class CompletionEvidenceTransportTests(unittest.TestCase):
    def test_encodes_and_decodes_one_bounded_canonical_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = b'{"schemaVersion":1}\n'
            evidence = root / "evidence.json"
            evidence.write_bytes(content)
            encoded = root / "evidence.txt"
            with mock.patch(
                "engineering_process.evidence_transport._validate",
                return_value={"changeId": "change-1"},
            ):
                details = encode_completion_evidence(
                    evidence,
                    encoded,
                    kind="receipt",
                )
            self.assertEqual("receipt", details["evidenceKind"])
            self.assertEqual(encoded.stat().st_size, details["encodedBytes"])
            output = root / "decoded.json"
            self.assertEqual(len(content), decode_completion_evidence(encoded, output))
            self.assertEqual(content, output.read_bytes())
            with self.assertRaisesRegex(ContractError, "refusing to replace"):
                decode_completion_evidence(encoded, output)

    def test_rejects_invalid_base64_gzip_and_expansion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "evidence.json"
            invalid_base64 = root / "base64.txt"
            invalid_base64.write_bytes(b"not base64!")
            with self.assertRaisesRegex(ContractError, "canonical base64"):
                decode_completion_evidence(invalid_base64, output)

            invalid_gzip = root / "gzip.txt"
            invalid_gzip.write_bytes(base64.b64encode(b"not gzip"))
            with self.assertRaisesRegex(ContractError, "valid gzip"):
                decode_completion_evidence(invalid_gzip, output)

            oversized = root / "oversized.txt"
            oversized.write_bytes(
                base64.b64encode(gzip.compress(b"x" * (MAX_RECEIPT_BYTES + 1), mtime=0))
            )
            with self.assertRaisesRegex(ContractError, "gzip boundary"):
                decode_completion_evidence(oversized, output)


if __name__ == "__main__":
    unittest.main()
