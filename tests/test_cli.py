import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from engineering_process.cli import main
from engineering_process.contracts import read_json, validate_process_lock


PROCESS_ROOT = Path(__file__).resolve().parent.parent


class CliTests(unittest.TestCase):
    def initialize_repository(self, root: Path) -> None:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "process-test@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Process Test"],
            cwd=root,
            check=True,
        )

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

    def test_only_automation_proposal_body_input_uses_the_new_bound(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            body = Path(directory) / "oversized.md"
            body.write_bytes(b"x" * 65_537)
            with (
                mock.patch(
                    "engineering_process.cli._proposal_policy_evidence",
                    return_value=SimpleNamespace(),
                ),
                mock.patch(
                    "engineering_process.cli.source_state",
                    return_value={
                        "dirty": False,
                        "checkpoint": "b" * 40,
                        "fingerprint": f"sha256:{'c' * 64}",
                    },
                ),
                contextlib.redirect_stdout(stdout),
            ):
                result = main(
                    [
                        "publication",
                        "validate-proposal",
                        "--project-root",
                        directory,
                        "--policy-evidence",
                        str(Path(directory) / "proposal.json"),
                        "--repository",
                        "example/project",
                        "--commit",
                        "b" * 40,
                        "--title",
                        "chore(deps): update dependency",
                        "--branch",
                        "automation/renovate/dependency",
                        "--target-branch",
                        "main",
                        "--base-commit",
                        "a" * 40,
                        "--state",
                        "draft",
                        "--body-file",
                        str(body),
                        "--verifier-repository",
                        "example/verifier",
                        "--verifier-commit",
                        "d" * 40,
                        "--json",
                    ]
                )

        self.assertEqual(2, result)
        self.assertIn("PR body exceeds 65536 bytes", stdout.getvalue())

        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as legacy_directory:
            legacy_body = Path(legacy_directory) / "oversized.md"
            legacy_body.write_bytes(b"x" * 65_537)
            with contextlib.redirect_stdout(stdout):
                legacy_result = main(
                    [
                        "publication",
                        "validate-pr",
                        "--title",
                        "chore(deps): update dependency",
                        "--branch",
                        "automation/renovate/dependency",
                        "--state",
                        "draft",
                        "--body-file",
                        str(legacy_body),
                        "--json",
                    ]
                )
        self.assertEqual(1, legacy_result)
        self.assertNotIn("PR body exceeds", stdout.getvalue())

    def test_routes_completed_source_publication_validation(self):
        checkpoint = "a" * 40
        fingerprint = f"sha256:{'b' * 64}"
        lifecycle = {
            "phase": "completed",
            "completion": {"path": "completion.json"},
            "current": True,
            "pendingFindings": [],
            "verification": [
                {
                    "checkpoint": checkpoint,
                    "workspaceFingerprint": fingerprint,
                }
            ],
        }
        source = {
            "dirty": False,
            "checkpoint": checkpoint,
            "fingerprint": fingerprint,
        }
        with tempfile.TemporaryDirectory() as directory:
            body = Path(directory) / "pr.md"
            body.write_text(
                "<!-- engineering-process:pr-description:start -->\n"
                "## Summary\n\nSummary.\n\n"
                "## Contract and scope\n\nContract.\n\n"
                "## Impact and risk\n\nRisk.\n\n"
                "## Verification\n\nVerified.\n\n"
                "## Independent review\n\nReviewed.\n\n"
                "## Requirements and rules followed\n\n"
                "- [x] **Scope and contract** — accepted scope is implemented without unapproved expansion. [status: satisfied]\n"
                "- [x] **Verification evidence** — required current profiles pass on the published checkpoint. [status: satisfied]\n"
                "- [x] **Independent review** — a separate reviewer approved the published checkpoint with no open required finding. [status: satisfied]\n"
                "<!-- engineering-process:pr-description:end -->\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with (
                mock.patch(
                    "engineering_process.cli.lifecycle_status",
                    return_value=lifecycle,
                ),
                mock.patch(
                    "engineering_process.cli.source_state",
                    return_value=source,
                ),
                contextlib.redirect_stdout(stdout),
            ):
                result = main(
                    [
                        "publication",
                        "validate-source",
                        "--project-root",
                        directory,
                        "--change-id",
                        "change-1",
                        "--commit",
                        checkpoint,
                        "--title",
                        "feat(process): standardize publication",
                        "--branch",
                        "feat/standardize-publication",
                        "--body-file",
                        str(body),
                        "--json",
                    ]
                )

        self.assertEqual(0, result)
        self.assertEqual(
            "publication validate-source",
            json.loads(stdout.getvalue())["command"],
        )

    def test_routes_external_evidence_source_publication_validation(self):
        checkpoint = "a" * 40
        fingerprint = f"sha256:{'b' * 64}"
        receipt = {
            "changeId": "change-1",
            "project": "sample",
            "checkpoint": checkpoint,
            "workspaceFingerprint": fingerprint,
            "sha256": f"sha256:{'c' * 64}",
        }
        source = {
            "dirty": False,
            "checkpoint": checkpoint,
            "fingerprint": fingerprint,
        }
        with tempfile.TemporaryDirectory() as directory:
            body = Path(directory) / "pr.md"
            body.write_text(
                "<!-- engineering-process:pr-description:start -->\n"
                "## Summary\n\nSummary.\n\n"
                "## Contract and scope\n\nContract.\n\n"
                "## Impact and risk\n\nRisk.\n\n"
                "## Verification\n\nVerified.\n\n"
                "## Independent review\n\nReviewed.\n\n"
                "## Requirements and rules followed\n\n"
                "- [x] **Scope and contract** — accepted scope is implemented without unapproved expansion. [status: satisfied]\n"
                "- [x] **Verification evidence** — required current profiles pass on the published checkpoint. [status: satisfied]\n"
                "- [x] **Independent review** — a separate reviewer approved the published checkpoint with no open required finding. [status: satisfied]\n"
                "<!-- engineering-process:pr-description:end -->\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with (
                mock.patch("engineering_process.cli.validate_receipt", return_value=receipt),
                mock.patch(
                    "engineering_process.cli.validate_project",
                    return_value=SimpleNamespace(identifier="sample"),
                ),
                mock.patch("engineering_process.cli.read_json", return_value={}),
                mock.patch("engineering_process.cli.source_state", return_value=source),
                contextlib.redirect_stdout(stdout),
            ):
                result = main(
                    [
                        "publication",
                        "validate-evidence-source",
                        "--project-root",
                        directory,
                        "--evidence",
                        str(Path(directory) / "receipt.json"),
                        "--evidence-kind",
                        "receipt",
                        "--commit",
                        checkpoint,
                        "--title",
                        "feat(process): standardize publication",
                        "--branch",
                        "feat/standardize-publication",
                        "--body-file",
                        str(body),
                        "--json",
                    ]
                )

        self.assertEqual(0, result)
        self.assertEqual(
            "publication validate-evidence-source",
            json.loads(stdout.getvalue())["command"],
        )

    def test_routes_completion_evidence_encoding(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence.json"
            output = root / "evidence.txt"
            with (
                mock.patch(
                    "engineering_process.cli.encode_completion_evidence",
                    return_value={
                        "changeId": "change-1",
                        "evidenceKind": "receipt",
                        "encodedBytes": 100,
                        "output": str(output),
                    },
                ),
                contextlib.redirect_stdout(stdout),
            ):
                result = main(
                    [
                        "evidence",
                        "encode-completion",
                        "--evidence",
                        str(evidence),
                        "--evidence-kind",
                        "receipt",
                        "--output",
                        str(output),
                        "--json",
                    ]
                )

        self.assertEqual(0, result)
        self.assertEqual(
            "evidence encode-completion",
            json.loads(stdout.getvalue())["command"],
        )

    def test_validates_project_adoption_migration_contract(self):
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            result = main(
                [
                    "contract",
                    "validate",
                    "--kind",
                    "adoption-migration",
                    str(PROCESS_ROOT / "examples" / "adoption-migration.json"),
                    "--json",
                ]
            )

        self.assertEqual(0, result)
        self.assertEqual(
            "adoption-migration", json.loads(stdout.getvalue())["kind"]
        )

    def test_validates_controlled_automation_proposal_contract(self):
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            result = main(
                [
                    "contract",
                    "validate",
                    "--kind",
                    "automation-proposal",
                    str(PROCESS_ROOT / "examples" / "automation-proposal.json"),
                    "--json",
                ]
            )

        self.assertEqual(0, result)
        self.assertEqual(
            "automation-proposal", json.loads(stdout.getvalue())["kind"]
        )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = main(
                [
                    "contract",
                    "validate",
                    "--kind",
                    "automation-proposal-policy",
                    str(
                        PROCESS_ROOT
                        / "examples"
                        / "automation-proposal-policy.json"
                    ),
                    "--json",
                ]
            )
        self.assertEqual(0, result)
        self.assertEqual(
            "automation-proposal-policy", json.loads(stdout.getvalue())["kind"]
        )

    def test_routes_proposal_and_exact_completion_validation(self):
        proposal = SimpleNamespace(
            automation_owner="renovate",
            base_sha="a" * 40,
            completion_check="lifecycle-completion",
            opt_in_sha256=f"sha256:{'e' * 64}",
            proposal_kind="dependency-update",
        )
        receipt = {
            "changeId": "change-1",
            "sha256": f"sha256:{'a' * 64}",
        }
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = root / "pr.md"
            body.write_text("ready body\n", encoding="utf-8")
            common = [
                "--project-root",
                directory,
                "--policy-evidence",
                str(root / "proposal.json"),
                "--repository",
                "example/project",
                "--commit",
                "b" * 40,
                "--title",
                "chore(deps): update dependency",
                "--branch",
                "automation/renovate/dependency",
                "--target-branch",
                "main",
                "--base-commit",
                "a" * 40,
                "--body-file",
                str(body),
                "--verifier-repository",
                "example/verifier",
                "--verifier-commit",
                "c" * 40,
                "--json",
            ]
            with (
                mock.patch(
                    "engineering_process.cli._proposal_policy_evidence",
                    return_value=proposal,
                ),
                mock.patch(
                    "engineering_process.cli.validate_controlled_automation_proposal",
                    return_value=[],
                ) as proposal_gate,
                mock.patch(
                    "engineering_process.cli.validate_controlled_automation_proposal_completion",
                    return_value=[],
                ) as completion_gate,
                mock.patch("engineering_process.cli.validate_receipt", return_value=receipt),
                mock.patch(
                    "engineering_process.cli.validate_project",
                    return_value=SimpleNamespace(identifier="example-project"),
                ),
                mock.patch("engineering_process.cli.read_json", return_value={}),
                mock.patch(
                    "engineering_process.cli.source_state",
                    return_value={
                        "dirty": False,
                        "checkpoint": "b" * 40,
                        "fingerprint": f"sha256:{'d' * 64}",
                    },
                ),
                contextlib.redirect_stdout(stdout),
            ):
                proposal_result = main(
                    ["publication", "validate-proposal", "--state", "draft", *common]
                )
                completion_result = main(
                    [
                        "publication",
                        "validate-proposal-completion",
                        "--evidence",
                        str(root / "receipt.json"),
                        "--evidence-kind",
                        "receipt",
                        *common,
                    ]
                )
                with (
                    self.assertRaises(SystemExit),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    main(
                        [
                            "publication",
                            "validate-proposal-completion",
                            "--evidence",
                            str(root / "authorization.json"),
                            "--evidence-kind",
                            "bootstrap-authorization",
                            *common,
                        ]
                    )

        self.assertEqual(0, proposal_result)
        self.assertEqual(0, completion_result)
        proposal_gate.assert_called_once()
        completion_gate.assert_called_once()
        self.assertIn("publication validate-proposal", stdout.getvalue())
        self.assertIn("publication validate-proposal-completion", stdout.getvalue())

    def test_validates_release_change_contract(self):
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            result = main(
                [
                    "contract",
                    "validate",
                    "--kind",
                    "release-change",
                    str(PROCESS_ROOT / "examples" / "release-change.json"),
                    "--json",
                ]
            )

        self.assertEqual(0, result)
        self.assertEqual("release-change", json.loads(stdout.getvalue())["kind"])

    def test_publication_plans_exact_version_from_change_types(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = main(
                [
                    "publication",
                    "plan-version",
                    "--previous-version",
                    "0.1.1",
                    "--change-type",
                    "fix",
                    "--change-type",
                    "capability",
                    "--json",
                ]
            )

        self.assertEqual(0, result)
        report = json.loads(stdout.getvalue())
        self.assertEqual("0.2.0", report["version"])
        self.assertEqual("minor", report["classification"])
        self.assertEqual("backward-compatible", report["compatibility"])
        self.assertEqual(["capability", "fix"], report["changeTypes"])

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
                    "publish-change",
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
                        "schemaVersion": 3,
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
                            "foregroundOnly": True,
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

    def test_verify_plan_only_reports_selection_without_environment_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            self.initialize_repository(project_root)
            manifest = project_root / "candidate.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schemaVersion": 3,
                        "project": "sample",
                        "lifecycle": {"requiredProfiles": ["development"]},
                        "impact": {
                            "baseRefs": ["main"],
                            "unmatchedPaths": "all-scoped-checks",
                            "components": [
                                {"id": "docs", "paths": ["**/*.md"], "affects": []},
                                {"id": "source", "paths": ["src/**"], "affects": []},
                            ],
                        },
                        "profiles": {
                            "development": [
                                {
                                    "id": "docs",
                                    "run": ["missing-doc-command"],
                                    "timeoutSeconds": 30,
                                    "components": ["docs"],
                                },
                                {
                                    "id": "source",
                                    "run": ["missing-source-command"],
                                    "timeoutSeconds": 30,
                                    "components": ["source"],
                                },
                            ]
                        },
                        "environment": {
                            "defaultProfile": "development",
                            "foregroundOnly": True,
                            "managedTools": [],
                            "profiles": {"development": ["unavailable"]},
                            "requirements": [
                                {
                                    "id": "unavailable",
                                    "description": "A probe plan-only must not execute",
                                    "probe": {
                                        "run": [sys.executable, "-c", "raise SystemExit(99)"],
                                        "timeoutSeconds": 15,
                                        "readOnly": True,
                                    },
                                    "remediation": "Not required for plan-only.",
                                }
                            ],
                            "setupActions": [],
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
            manifest.unlink()
            subprocess.run(["git", "add", "."], cwd=project_root, check=True)
            subprocess.run(["git", "commit", "-qm", "bootstrap"], cwd=project_root, check=True)
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=project_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (project_root / "src").mkdir()
            (project_root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = main(
                    [
                        "verify",
                        *common,
                        "--profile",
                        "development",
                        "--plan-only",
                        "--base-ref",
                        base,
                    ]
                )

            self.assertEqual(result, 0)
            plan = json.loads(output.getvalue())
            self.assertEqual(plan["selectedCheckIds"], ["source"])
            self.assertEqual(plan["skippedCheckIds"], ["docs"])
