import json
import tempfile
import unittest
from pathlib import Path

from verification.classify_release_preparation import (
    MAX_RESULT_BYTES,
    ReleasePreparationError,
    STALE_SELF_ADOPTION_RESULT,
    classify_result,
    read_result,
)


class ReleasePreparationTests(unittest.TestCase):
    def test_exact_stale_self_adoption_failure_is_deferred(self):
        self.assertEqual(
            "deferred-self-adoption",
            classify_result(dict(STALE_SELF_ADOPTION_RESULT)),
        )

    def test_every_other_failure_remains_blocking(self):
        candidates = (
            {**STALE_SELF_ADOPTION_RESULT, "extra": True},
            {**STALE_SELF_ADOPTION_RESULT, "status": "passed"},
            {**STALE_SELF_ADOPTION_RESULT, "errors": ["different failure"]},
            {
                **STALE_SELF_ADOPTION_RESULT,
                "errors": [
                    *STALE_SELF_ADOPTION_RESULT["errors"],
                    "another failure",
                ],
            },
        )

        for candidate in candidates:
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(
                    ReleasePreparationError, "not the exact"
                ):
                    classify_result(candidate)

    def test_reader_rejects_oversized_and_invalid_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            oversized = root / "oversized.json"
            oversized.write_bytes(b"x" * (MAX_RESULT_BYTES + 1))
            invalid = root / "invalid.json"
            invalid.write_text("not json", encoding="utf-8")

            with self.assertRaisesRegex(ReleasePreparationError, "size limit"):
                read_result(oversized)
            with self.assertRaisesRegex(ReleasePreparationError, "invalid JSON"):
                read_result(invalid)

    def test_reader_accepts_the_bounded_exact_document(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(
                json.dumps(STALE_SELF_ADOPTION_RESULT),
                encoding="utf-8",
            )

            self.assertEqual(STALE_SELF_ADOPTION_RESULT, read_result(path))


if __name__ == "__main__":
    unittest.main()
