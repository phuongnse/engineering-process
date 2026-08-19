import contextlib
import io
import json
import sys
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

    def test_setup_cli_plans_then_requires_explicit_mutation_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            manifest = project_root / "candidate.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "project": "sample",
                        "lifecycle": {
                            "requiredProfiles": ["development", "review"]
                        },
                        "profiles": {
                            profile: [
                                {
                                    "id": profile,
                                    "run": [
                                        sys.executable,
                                        "-c",
                                        "raise SystemExit(0)",
                                    ],
                                    "timeoutSeconds": 30,
                                }
                            ]
                            for profile in ("development", "review")
                        },
                        "environment": {
                            "defaultProfile": "development",
                            "managedTools": [],
                            "profiles": {
                                "development": ["ready"],
                                "review": ["ready"],
                            },
                            "requirements": [
                                {
                                    "id": "ready",
                                    "description": "Ready marker",
                                    "probe": {
                                        "run": [
                                            sys.executable,
                                            "-c",
                                            "from pathlib import Path; "
                                            "raise SystemExit(not Path('ready').is_file())",
                                        ],
                                        "timeoutSeconds": 30,
                                        "readOnly": True,
                                    },
                                    "remediation": "Create the ready marker.",
                                    "setupAction": "prepare",
                                }
                            ],
                            "setupActions": [
                                {
                                    "id": "prepare",
                                    "kind": "command",
                                    "run": [
                                        sys.executable,
                                        "-c",
                                        "from pathlib import Path; "
                                        "Path('ready').write_text('ready')",
                                    ],
                                    "timeoutSeconds": 30,
                                    "mutations": ["project-files"],
                                }
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )
            common = [
                "--project-root",
                str(project_root),
                "--process-root",
                str(PROCESS_ROOT),
                "--json",
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(["project", "init", *common, "--manifest", str(manifest)]),
                    0,
                )
                self.assertEqual(main(["setup", *common]), 0)
                self.assertEqual(main(["setup", *common, "--apply"]), 1)
            self.assertFalse((project_root / "ready").exists())

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "setup",
                            *common,
                            "--apply",
                            "--allow",
                            "project-files",
                        ]
                    ),
                    0,
                )
            self.assertTrue((project_root / "ready").is_file())

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "exec",
                            *common,
                            "--profile",
                            "development",
                            "--timeout-seconds",
                            "30",
                            "--",
                            sys.executable,
                            "-c",
                            "print('project command')",
                        ]
                    ),
                    0,
                )
            report = json.loads(output.getvalue())
            self.assertEqual("passed", report["status"])
            self.assertIn("project command", report["execution"]["stdout"])
