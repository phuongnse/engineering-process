import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from engineering_process.cli import main
from engineering_process.contracts import read_json, validate_process_lock


PROCESS_ROOT = Path(__file__).resolve().parent.parent


class CliTests(unittest.TestCase):
    def test_routes_portable_publication_validation(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "publication",
                        "validate-branch",
                        "--branch",
                        "feat/portable-publication",
                        "--json",
                    ]
                ),
                0,
            )

    def test_creates_core_lock_and_refuses_implicit_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            arguments = [
                "lock",
                "create",
                "--project-root",
                str(project_root),
                "--process-root",
                str(PROCESS_ROOT),
                "--json",
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(arguments), 0)

            lock = validate_process_lock(
                read_json(project_root / ".process" / "process.lock")
            )
            self.assertEqual(
                lock.skills,
                (
                    "define-change-contract",
                    "evolve-process",
                    "finish-change",
                    "implement-change",
                    "plan-change",
                    "review-change",
                    "run-change",
                    "verify-change",
                ),
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(arguments), 2)

    def test_explicit_bundle_still_includes_mandatory_core(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            with contextlib.redirect_stdout(io.StringIO()):
                result = main(
                    [
                        "lock",
                        "create",
                        "--project-root",
                        str(project_root),
                        "--process-root",
                        str(PROCESS_ROOT),
                        "--bundle",
                        "cross-repo",
                        "--json",
                    ]
                )

            self.assertEqual(result, 0)
            lock = validate_process_lock(
                read_json(project_root / ".process" / "process.lock")
            )
            self.assertIn("cross-repo-change", lock.skills)
            self.assertIn("run-change", lock.skills)

    def test_lock_validate_rejects_a_schema_valid_lock_without_core(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            process = project_root / ".process"
            process.mkdir()
            (process / "process.lock").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "process": {
                            "version": "0.1.0",
                            "digest": "sha256:" + "0" * 64,
                        },
                        "skills": ["assess-design"],
                    }
                ),
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                result = main(
                    [
                        "lock",
                        "validate",
                        "--project-root",
                        str(project_root),
                        "--process-root",
                        str(PROCESS_ROOT),
                        "--json",
                    ]
                )

            self.assertEqual(2, result)
