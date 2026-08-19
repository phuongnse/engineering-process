import os
import io
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from engineering_process.contracts import ContractError, validate_project
from engineering_process.environment import (
    doctor_environment,
    execute_command,
    setup_environment,
)
from engineering_process.tooling import ManagedCommandBinding, platform_identifier


def project_document(*, setup: bool = True, dependency: bool = False):
    ready_probe = (
        "from pathlib import Path; "
        "raise SystemExit(0 if Path('ready.txt').is_file() else 1)"
    )
    actions = []
    if dependency:
        actions.append(
            {
                "id": "prepare-parent",
                "kind": "command",
                "run": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('parent.txt').write_text('ready')",
                ],
                "timeoutSeconds": 30,
                "mutations": ["project-files"],
            }
        )
    if setup:
        command = "from pathlib import Path; "
        if dependency:
            command += "assert Path('parent.txt').is_file(); "
        command += "Path('ready.txt').write_text('ready')"
        action = {
            "id": "prepare-environment",
            "kind": "command",
            "run": [sys.executable, "-c", command],
            "timeoutSeconds": 30,
            "mutations": ["project-files"],
        }
        if dependency:
            action["requires"] = ["prepare-parent"]
        actions.append(action)
    actions.sort(key=lambda item: item["id"])
    requirement = {
        "id": "project-environment",
        "description": "Project environment marker",
        "probe": {
            "run": [sys.executable, "-c", ready_probe],
            "timeoutSeconds": 30,
            "readOnly": True,
        },
        "remediation": "Run the declared project setup action.",
    }
    if setup:
        requirement["setupAction"] = "prepare-environment"
    return {
        "schemaVersion": 3,
        "project": "sample",
        "lifecycle": {"requiredProfiles": ["development", "review"]},
        "profiles": {
            "development": [
                {
                    "id": "unit",
                    "run": [sys.executable, "-c", "raise SystemExit(0)"],
                    "timeoutSeconds": 30,
                }
            ],
            "review": [
                {
                    "id": "review",
                    "run": [sys.executable, "-c", "raise SystemExit(0)"],
                    "timeoutSeconds": 30,
                }
            ],
        },
        "environment": {
            "defaultProfile": "development",
            "foregroundOnly": True,
            "managedTools": [],
            "profiles": {
                "development": ["project-environment"],
                "review": ["project-environment"],
            },
            "requirements": [requirement],
            "setupActions": actions,
        },
    }


class EnvironmentTests(unittest.TestCase):
    def test_managed_command_binding_preserves_logical_evidence_without_a_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "tool.py"
            script.write_text(
                "import sys; print('|'.join(sys.argv[1:]))\n",
                encoding="utf-8",
            )

            report = execute_command(
                root,
                identifier="managed-alias",
                run=("sample-tool", "one", "two"),
                timeout_seconds=30,
                working_directory=".",
                command_bindings={
                    "sample-tool": ManagedCommandBinding(
                        application=Path(sys.executable),
                        prefix_arguments=(str(script),),
                    )
                },
            )

            self.assertEqual("passed", report["status"])
            self.assertEqual(["sample-tool", "one", "two"], report["command"])
            self.assertEqual(f"one|two{os.linesep}", report["stdout"])

    def test_streamed_output_is_bounded_but_full_digest_is_recorded(self):
        class Sink:
            def __init__(self):
                self.buffer = io.BytesIO()

            def write(self, value):
                return self.buffer.write(value.encode("utf-8"))

            def flush(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sink = Sink()
            with patch("engineering_process.environment.sys.stderr", sink):
                report = execute_command(
                    root,
                    identifier="bounded-log",
                    run=(sys.executable, "-c", "print('x' * 1100000)"),
                    timeout_seconds=30,
                    working_directory=".",
                    stream_output=True,
                )

            self.assertEqual("passed", report["status"])
            self.assertGreater(report["stdoutBytes"], 1_000_000)
            self.assertTrue(report["outputTruncated"])
            self.assertTrue(report["streamOutputTruncated"])
            self.assertLess(len(sink.buffer.getvalue()), 1_000_100)

    def test_doctor_is_read_only_and_reports_missing_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = validate_project(project_document())

            report = doctor_environment(root, project)

            self.assertEqual("failed", report["status"])
            self.assertEqual("missing", report["requirements"][0]["status"])
            self.assertFalse((root / "ready.txt").exists())

    def test_setup_plan_does_not_mutate_and_lists_required_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = validate_project(project_document())

            report = setup_environment(
                root,
                project,
                profile=None,
                apply=False,
                allowed_mutations=set(),
            )

            self.assertEqual("planned", report["status"])
            self.assertEqual(["project-files"], report["requiredApprovals"])
            self.assertEqual("planned", report["actions"][0]["status"])
            self.assertFalse((root / "ready.txt").exists())

    def test_setup_preflights_all_mutation_scopes_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = validate_project(project_document())

            report = setup_environment(
                root,
                project,
                profile=None,
                apply=True,
                allowed_mutations=set(),
            )

            self.assertEqual("blocked", report["status"])
            self.assertIn("unapproved mutation scopes", report["blocked"][0])
            self.assertFalse((root / "ready.txt").exists())

    def test_setup_preflights_every_working_directory_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = project_document(dependency=True)
            document["environment"]["setupActions"][1][
                "workingDirectory"
            ] = "missing"
            project = validate_project(document)

            report = setup_environment(
                root,
                project,
                profile=None,
                apply=True,
                allowed_mutations={"project-files"},
            )

            self.assertEqual("blocked", report["status"])
            self.assertIn("working directory does not exist", report["blocked"][0])
            self.assertFalse((root / "parent.txt").exists())
            self.assertFalse((root / "ready.txt").exists())

    def test_setup_applies_dependency_order_and_reprobes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = validate_project(project_document(dependency=True))

            report = setup_environment(
                root,
                project,
                profile="development",
                apply=True,
                allowed_mutations={"project-files"},
            )

            self.assertEqual("passed", report["status"])
            self.assertEqual(
                ["prepare-parent", "prepare-environment"],
                [action["id"] for action in report["actions"]],
            )
            self.assertEqual("passed", report["final"]["status"])
            self.assertTrue((root / "ready.txt").is_file())

    def test_missing_requirement_without_setup_action_is_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = validate_project(project_document(setup=False))

            report = setup_environment(
                root,
                project,
                profile=None,
                apply=False,
                allowed_mutations=set(),
            )

            self.assertEqual("blocked", report["status"])
            self.assertIn("Run the declared project setup action", report["blocked"][0])

    def test_probe_output_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = project_document()
            probe = document["environment"]["requirements"][0]["probe"]
            probe["run"] = [sys.executable, "-c", "print('x' * 50000)"]
            probe["outputRegex"] = "^x+"
            probe["outputStream"] = "stdout"
            project = validate_project(document)

            report = doctor_environment(root, project)
            requirement = report["requirements"][0]

            self.assertEqual("passed", report["status"])
            self.assertTrue(requirement["outputTruncated"])
            self.assertLessEqual(len(requirement["stdout"].encode()), 16_384)

    def test_probe_output_regex_is_bounded_by_the_probe_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = project_document()
            probe = document["environment"]["requirements"][0]["probe"]
            probe["run"] = [
                sys.executable,
                "-c",
                "print('a' * 16000 + '!')",
            ]
            probe["outputRegex"] = "(a+)+$"
            probe["outputStream"] = "stdout"
            probe["timeoutSeconds"] = 1
            project = validate_project(document)

            started = time.monotonic()
            report = doctor_environment(root, project)
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 1)
            requirement = report["requirements"][0]
            self.assertEqual("missing", requirement["status"])
            self.assertIn("bounded match timeout", requirement["outputMatchError"])

    @unittest.skipUnless(os.name == "posix", "POSIX process-group regression")
    def test_probe_terminates_descendant_that_ignores_sigterm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_pid = root / "child.pid"
            child_code = (
                "import os, signal, sys, time; "
                "from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "Path(sys.argv[1]).write_text(str(os.getpid())); "
                "time.sleep(60)"
            )
            parent_code = (
                "import subprocess, sys, time; "
                "from pathlib import Path; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
                "p=Path(sys.argv[2]); "
                "deadline=time.monotonic()+5; "
                "\nwhile not p.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
                "raise SystemExit(0 if p.exists() else 2)"
            )
            document = project_document()
            probe = document["environment"]["requirements"][0]["probe"]
            probe["run"] = [
                sys.executable,
                "-c",
                parent_code,
                child_code,
                str(child_pid),
            ]
            probe["timeoutSeconds"] = 10
            project = validate_project(document)

            started = time.monotonic()
            report = doctor_environment(root, project)
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 5)
            requirement = report["requirements"][0]
            self.assertEqual("missing", requirement["status"])
            self.assertIn("descendant processes", requirement["error"])
            pid = int(child_pid.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                status_path = Path(f"/proc/{pid}/stat")
                if status_path.is_file() and status_path.read_text().split()[2] == "Z":
                    break
                time.sleep(0.02)
            else:
                self.fail(f"descendant process {pid} survived bounded termination")

    def test_manifest_requires_the_single_environment_contract(self):
        document = project_document()
        del document["environment"]
        with self.assertRaisesRegex(ContractError, "missing properties: environment"):
            validate_project(document)

    def test_environment_rejects_cycles_and_undefined_references(self):
        document = project_document(dependency=True)
        document["environment"]["setupActions"][0]["requires"] = [
            "prepare-environment"
        ]
        with self.assertRaisesRegex(ContractError, "dependency cycle"):
            validate_project(document)

        document = project_document()
        document["environment"]["profiles"]["development"] = ["missing"]
        with self.assertRaisesRegex(ContractError, "undefined requirements"):
            validate_project(document)

    def test_managed_tool_action_derives_scopes_and_validates_artifact(self):
        document = project_document(setup=False)
        document["environment"]["managedTools"] = [
            {
                "id": "sample",
                "version": "1.2.3",
                "artifacts": [
                    {
                        "platform": "linux-glibc-x64",
                        "url": "https://downloads.example.test/sample.tar.gz",
                        "checksum": f"sha256:{'0' * 64}",
                        "archiveFormat": "tar.gz",
                        "stripComponents": 1,
                        "maxDownloadBytes": 1000,
                        "maxExtractedBytes": 2000,
                        "maxFiles": 20,
                        "commands": {"sample": "bin/sample"},
                    }
                ],
            }
        ]
        document["environment"]["setupActions"] = [
            {
                "id": "install-sample",
                "kind": "managed-tool",
                "tool": "sample",
                "timeoutSeconds": 300,
            }
        ]
        document["environment"]["requirements"][0][
            "setupAction"
        ] = "install-sample"

        project = validate_project(document)

        action = project.environment.setup_actions["install-sample"]
        self.assertEqual(("network", "user-files"), action.mutations)
        self.assertEqual("sample", action.tool)

        document["environment"]["managedTools"][0]["artifacts"][0][
            "url"
        ] = "http://downloads.example.test/sample.tar.gz"
        with self.assertRaisesRegex(ContractError, "must be an HTTPS URL"):
            validate_project(document)

        document["environment"]["managedTools"][0]["artifacts"][0][
            "url"
        ] = "https://downloads.example.test:notaport/sample.tar.gz"
        with self.assertRaisesRegex(ContractError, "invalid HTTPS URL"):
            validate_project(document)

        document["environment"]["managedTools"][0]["artifacts"][0][
            "url"
        ] = "https://downloads.example.test\\@mirror.example.test/sample.tar.gz"
        with self.assertRaisesRegex(ContractError, "printable ASCII URI"):
            validate_project(document)

    def managed_setup_document(self):
        document = project_document(setup=False, dependency=False)
        platform_name = platform_identifier()
        command_path = (
            "sample.exe" if platform_name.startswith("windows-") else "sample"
        )
        document["environment"]["managedTools"] = [
            {
                "id": "sample",
                "version": "1.2.3",
                "artifacts": [
                    {
                        "platform": platform_name,
                        "url": "https://downloads.example.test/sample",
                        "checksum": f"sha256:{'0' * 64}",
                        "archiveFormat": "file",
                        "stripComponents": 0,
                        "maxDownloadBytes": 1000,
                        "maxExtractedBytes": 1000,
                        "maxFiles": 1,
                        "commands": {"sample": command_path},
                    }
                ],
            }
        ]
        document["environment"]["setupActions"] = [
            {
                "id": "a-prepare",
                "kind": "command",
                "run": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('mutated').write_text('yes')",
                ],
                "timeoutSeconds": 30,
                "mutations": ["project-files"],
            },
            {
                "id": "z-install",
                "kind": "managed-tool",
                "tool": "sample",
                "timeoutSeconds": 30,
                "requires": ["a-prepare"],
            },
        ]
        document["environment"]["requirements"][0]["setupAction"] = "z-install"
        return document

    def test_setup_preflights_managed_install_target_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools_root = root / "managed-tools"
            invalid_target = tools_root / "sample" / "1.2.3"
            invalid_target.parent.mkdir(parents=True)
            invalid_target.write_text("occupied", encoding="utf-8")
            project = validate_project(self.managed_setup_document())

            with patch(
                "engineering_process.tooling.managed_tools_root",
                return_value=tools_root,
            ):
                report = setup_environment(
                    root,
                    project,
                    profile=None,
                    apply=True,
                    allowed_mutations={"network", "project-files", "user-files"},
                )

            self.assertEqual("blocked", report["status"])
            self.assertIn("not a directory", "\n".join(report["blocked"]))
            self.assertFalse((root / "mutated").exists())

    def test_setup_preserves_partial_evidence_for_operational_installer_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools_root = root / "managed-tools"
            project = validate_project(self.managed_setup_document())

            with (
                patch(
                    "engineering_process.tooling.managed_tools_root",
                    return_value=tools_root,
                ),
                patch(
                    "engineering_process.environment.install_managed_tool",
                    side_effect=ValueError("operational failure"),
                ),
            ):
                report = setup_environment(
                    root,
                    project,
                    profile=None,
                    apply=True,
                    allowed_mutations={"network", "project-files", "user-files"},
                )

            self.assertEqual("failed", report["status"])
            self.assertEqual(["a-prepare", "z-install"], [
                action["id"] for action in report["actions"]
            ])
            self.assertEqual("ValueError", report["actions"][1]["errorType"])
            self.assertTrue((root / "mutated").is_file())


if __name__ == "__main__":
    unittest.main()
