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

from engineering_process.cli import (
    _require_installed_transition_authority,
    _transition_processctl_command,
    _transition_source_authority,
    main,
)
from engineering_process.contracts import ContractError, read_json, validate_process_lock


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

    @staticmethod
    def bounded_result(stdout: bytes, *, returncode: int = 0):
        return SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=b"",
            timed_out=False,
            output_exceeded=False,
            descendants_found=False,
            cleanup_error=None,
            input_error=False,
        )

    def test_transition_authority_uses_exact_bounded_n1_probes(self):
        root = Path("/authority")
        project = Path("/project")
        lock = SimpleNamespace(
            version="0.7.0",
            digest=f"sha256:{'7' * 64}",
            skills=("run-change",),
        )
        results = [
            self.bounded_result(b"0.7.0\n"),
            self.bounded_result(
                json.dumps(
                    {
                        "status": "passed",
                        "digest": lock.digest,
                        "skills": ["run-change"],
                    }
                ).encode("utf-8")
            ),
            self.bounded_result(
                json.dumps(
                    {
                        "status": "passed",
                        "processVersion": "0.7.0",
                        "project": "sample-project",
                        "issues": [],
                    }
                ).encode("utf-8")
            ),
        ]
        with (
            mock.patch(
                "engineering_process.cli._transition_authority_commands",
                return_value=(root / "bin/python", root / "bin/processctl"),
            ),
            mock.patch(
                "engineering_process.cli.run_bounded_process",
                side_effect=results,
            ) as bounded,
        ):
            _require_installed_transition_authority(
                project,
                root,
                lock,
                {"version": "0.7.0", "digest": lock.digest},
                "sample-project",
            )

        self.assertEqual(3, bounded.call_count)
        for call in bounded.call_args_list:
            self.assertEqual(15, call.kwargs["timeout_seconds"])
            self.assertEqual(65_536, call.kwargs["max_stream_bytes"])
            self.assertEqual(65_536, call.kwargs["max_total_bytes"])
        self.assertEqual(root, bounded.call_args_list[0].kwargs["working_directory"])
        self.assertEqual(project, bounded.call_args_list[1].kwargs["working_directory"])
        self.assertEqual(project, bounded.call_args_list[2].kwargs["working_directory"])
        self.assertEqual(
            [str(root / "bin/python"), str(root / "bin/processctl"), "digest", "--json"],
            bounded.call_args_list[1].args[0],
        )
        self.assertEqual(
            ["C:/authority/Scripts/processctl.exe", "digest", "--json"],
            _transition_processctl_command(
                Path("C:/authority/Scripts/python.exe"),
                Path("C:/authority/Scripts/processctl.exe"),
                "digest",
                "--json",
            ),
        )

    def test_transition_authority_rejects_identity_and_probe_failures(self):
        lock = SimpleNamespace(
            version="0.7.0",
            digest=f"sha256:{'7' * 64}",
            skills=("run-change",),
        )
        with self.assertRaisesRegex(ContractError, "does not match process.lock"):
            _require_installed_transition_authority(
                Path("/project"),
                Path("/authority"),
                lock,
                {"version": "0.8.0", "digest": lock.digest},
                "sample-project",
            )

    def test_transition_authority_executes_installed_style_n1_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "authority"
            package = root / "engineering_process"
            scripts = root / "bin"
            package.mkdir(parents=True)
            scripts.mkdir()
            (package / "__init__.py").write_text(
                'VERSION = "0.7.0"\n', encoding="utf-8"
            )
            processctl = scripts / "processctl"
            digest = f"sha256:{'7' * 64}"
            processctl.write_text(
                "import json, sys\n"
                "if sys.argv[1] == 'digest':\n"
                f"    print(json.dumps({{'status':'passed','digest':'{digest}','skills':['run-change']}}))\n"
                "elif sys.argv[1] == 'doctor':\n"
                "    print(json.dumps({'status':'passed','processVersion':'0.7.0','project':'sample-project','issues':[]}))\n"
                "else:\n"
                "    raise SystemExit(2)\n",
                encoding="utf-8",
            )
            lock = SimpleNamespace(
                version="0.7.0", digest=digest, skills=("run-change",)
            )
            with mock.patch(
                "engineering_process.cli._transition_authority_commands",
                return_value=(Path(sys.executable), processctl),
            ):
                _require_installed_transition_authority(
                    Path(directory),
                    root,
                    lock,
                    {"version": "0.7.0", "digest": digest},
                    "sample-project",
                )

        failed = self.bounded_result(b"", returncode=1)
        with (
            mock.patch(
                "engineering_process.cli._transition_authority_commands",
                return_value=(Path("/authority/bin/python"), Path("/authority/bin/processctl")),
            ),
            mock.patch(
                "engineering_process.cli.run_bounded_process", return_value=failed
            ),
            self.assertRaisesRegex(ContractError, "bounded execution"),
        ):
            _require_installed_transition_authority(
                Path("/project"),
                Path("/authority"),
                lock,
                {"version": "0.7.0", "digest": lock.digest},
                "sample-project",
            )

    def test_transition_source_authority_routes_only_registered_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = json.loads(
                (PROCESS_ROOT / "examples" / "authority-transition-request.json").read_text(
                    encoding="utf-8"
                )
            )
            request_path = root / "request.json"
            request_path.write_text(json.dumps(request) + "\n", encoding="utf-8")
            args = SimpleNamespace(
                change_transition_command="register",
                request=request_path,
                project_root=root,
            )
            self.assertEqual(
                request["source"]["authority"], _transition_source_authority(args)
            )

            run = root / ".process" / "runs" / request["changeId"]
            run.mkdir(parents=True)
            copied = run / "authority-transition-request-1.json"
            copied.write_text(json.dumps(request) + "\n", encoding="utf-8")
            (run / "state.json").write_text(
                json.dumps(
                    {
                        "authorityTransition": {
                            "request": {
                                "path": copied.relative_to(root).as_posix()
                            }
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            later = SimpleNamespace(
                change_transition_command=None,
                change_id=request["changeId"],
                project_root=root,
            )
            self.assertEqual(
                request["source"]["authority"],
                _transition_source_authority(later),
            )
            ordinary = SimpleNamespace(
                change_transition_command=None,
                change_id="ordinary-change",
                project_root=root,
            )
            self.assertIsNone(_transition_source_authority(ordinary))

    def test_cli_registers_transition_with_installed_style_n1_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            authority = root / "authority"
            target = root / "target"
            artifacts = root / "artifacts"
            for path in (project / ".process", authority / "bin", target, artifacts):
                path.mkdir(parents=True)
            (authority / "engineering_process").mkdir()
            (authority / "engineering_process" / "__init__.py").write_text(
                'VERSION = "0.7.0"\n', encoding="utf-8"
            )
            processctl = authority / "bin" / "processctl"
            digest = f"sha256:{'7' * 64}"
            processctl.write_text(
                "import json, sys\n"
                "if sys.argv[1] == 'digest':\n"
                f" print(json.dumps({{'status':'passed','digest':'{digest}','skills':['run-change']}}))\n"
                "elif sys.argv[1] == 'doctor':\n"
                " print(json.dumps({'status':'passed','processVersion':'0.7.0','project':'engineering-process','issues':[]}))\n"
                "else: raise SystemExit(2)\n",
                encoding="utf-8",
            )
            (project / ".process" / "project.json").write_bytes(
                (PROCESS_ROOT / ".process" / "project.json").read_bytes()
            )
            (project / ".process" / "process.lock").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "process": {"version": "0.7.0", "digest": digest},
                        "skills": ["run-change"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            request = json.loads(
                (PROCESS_ROOT / "examples" / "authority-transition-request.json").read_text(
                    encoding="utf-8"
                )
            )
            request["project"] = "engineering-process"
            request["changeId"] = "adopt-process-0-9-0"
            request["source"]["authority"] = {
                "version": "0.7.0",
                "digest": digest,
            }
            request_path = root / "request.json"
            request_path.write_text(json.dumps(request) + "\n", encoding="utf-8")
            state = {
                "changeId": request["changeId"],
                "phase": "implementing",
                "cycle": 1,
                "revision": 3,
                "authorityTransition": {
                    "request": {"path": "request.json"}
                },
            }
            stdout = io.StringIO()
            with (
                mock.patch(
                    "engineering_process.cli._transition_authority_commands",
                    return_value=(Path(sys.executable), processctl),
                ),
                mock.patch(
                    "engineering_process.cli.lifecycle_environment_issues",
                    return_value=[],
                ),
                mock.patch(
                    "engineering_process.cli.register_authority_transition",
                    return_value=(state, request),
                ) as register,
                contextlib.redirect_stdout(stdout),
            ):
                result = main(
                    [
                        "change", "transition", "register",
                        "--project-root", str(project),
                        "--process-root", str(authority),
                        "--actor", "coordinator",
                        "--context", "control-context",
                        "--actor-kind", "agent",
                        "--change-id", request["changeId"],
                        "--request", str(request_path),
                        "--target-checkout", str(target),
                        "--artifact-root", str(artifacts),
                        "--release-receipt", str(root / "receipt.json"),
                        "--artifact-attestation", str(root / "attestation.json"),
                        "--json",
                    ]
                )
            self.assertEqual(0, result, stdout.getvalue())
            self.assertEqual(1, register.call_count)
            self.assertEqual(
                "change transition register",
                json.loads(stdout.getvalue())["command"],
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

    def test_routes_portable_federated_improvement_chain(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                [
                    "improvement",
                    "validate-chain",
                    "--signal",
                    str(PROCESS_ROOT / "examples" / "improvement-signal.json"),
                    "--disposition",
                    str(
                        PROCESS_ROOT
                        / "examples"
                        / "improvement-disposition.json"
                    ),
                    "--resolution",
                    str(
                        PROCESS_ROOT / "examples" / "improvement-resolution.json"
                    ),
                    "--reproduction",
                    str(
                        PROCESS_ROOT
                        / "examples"
                        / "improvement-reproduction.json"
                    ),
                    "--catalog",
                    str(PROCESS_ROOT / "examples" / "improvement-catalog.json"),
                    "--json",
                ]
            )

        self.assertEqual(0, result)
        report = json.loads(output.getvalue())
        self.assertEqual("closed", report["phase"])
        self.assertTrue(report["closed"])
        self.assertIsNone(report["nextOwner"])

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

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = main(
                [
                    "contract",
                    "validate",
                    "--kind",
                    "automation-policy",
                    str(PROCESS_ROOT / "examples" / "automation-policy.json"),
                    "--json",
                ]
            )
        self.assertEqual(0, result)
        self.assertEqual("automation-policy", json.loads(stdout.getvalue())["kind"])

    def test_routes_proposal_and_exact_completion_validation(self):
        proposal = SimpleNamespace(
            automation_owner="renovate",
            base_sha="a" * 40,
            completion_check="lifecycle-completion",
            consumer_owner_merge_required=False,
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
                    "cross-repo-change",
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
                        "delivery",
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
