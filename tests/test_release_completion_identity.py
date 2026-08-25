import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from engineering_process.contracts import ContractError
from verification.validate_release_completion_identity import (
    validate_release_completion_identity,
)


PROCESS_ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = (
    PROCESS_ROOT / "verification" / "validate_release_completion_identity.py"
)


class ReleaseCompletionIdentityTests(unittest.TestCase):
    def documents(self):
        summary = {
            "status": "passed",
            "changeId": "release-0-5-0",
            "checkpoint": "c" * 40,
            "comparisonBase": "b" * 40,
            "processVersion": "0.4.0",
            "processDigest": f"sha256:{'d' * 64}",
            "project": "engineering-process",
        }
        release_change = {
            "id": "release-0-5-0",
            "comparisonBase": "b" * 40,
            "affectedProjects": ["engineering-process"],
        }
        process_lock = {
            "process": {
                "version": "0.4.0",
                "digest": f"sha256:{'d' * 64}",
            }
        }
        return summary, release_change, process_lock

    def test_accepts_a_lifecycle_base_distinct_from_the_candidate_parent(self):
        summary, release_change, process_lock = self.documents()
        candidate_parent = "a" * 40

        result = validate_release_completion_identity(
            completion_summary=summary,
            release_change=release_change,
            process_lock=process_lock,
            expected_checkpoint="c" * 40,
        )

        self.assertNotEqual(candidate_parent, result["lifecycleComparisonBase"])
        self.assertEqual("b" * 40, result["lifecycleComparisonBase"])
        self.assertEqual("valid", result["status"])

    def test_rejects_each_identity_mismatch(self):
        mutations = {
            "changeId": "other-change",
            "checkpoint": "e" * 40,
            "comparisonBase": "e" * 40,
            "processVersion": "0.3.0",
            "processDigest": f"sha256:{'e' * 64}",
            "project": "other-project",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                summary, release_change, process_lock = self.documents()
                summary[field] = value
                with self.assertRaisesRegex(ContractError, field):
                    validate_release_completion_identity(
                        completion_summary=summary,
                        release_change=release_change,
                        process_lock=process_lock,
                        expected_checkpoint="c" * 40,
                    )

    def test_cli_boundary_reads_the_owned_documents(self):
        summary, release_change, process_lock = self.documents()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {}
            for name, document in (
                ("summary", summary),
                ("change", release_change),
                ("lock", process_lock),
            ):
                path = root / f"{name}.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                paths[name] = path

            result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR),
                    "--summary",
                    str(paths["summary"]),
                    "--release-change",
                    str(paths["change"]),
                    "--process-lock",
                    str(paths["lock"]),
                    "--expected-checkpoint",
                    "c" * 40,
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("valid", json.loads(result.stdout)["status"])


if __name__ == "__main__":
    unittest.main()
