import sys
import tempfile
import unittest
from pathlib import Path

from engineering_process.contracts import ContractError, validate_project
from engineering_process.environment import doctor_environment, setup_environment


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
        "schemaVersion": 2,
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
            "profiles": {
                "development": ["project-environment"],
                "review": ["project-environment"],
            },
            "requirements": [requirement],
            "setupActions": actions,
        },
    }


class EnvironmentTests(unittest.TestCase):
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

    def test_schema_one_remains_readable_but_cannot_run_setup(self):
        document = project_document()
        document["schemaVersion"] = 1
        del document["environment"]
        project = validate_project(document)
        self.assertEqual("not-declared", doctor_environment(Path.cwd(), project)["status"])
        with self.assertRaisesRegex(ContractError, "schema-version-2"):
            setup_environment(
                Path.cwd(),
                project,
                profile=None,
                apply=False,
                allowed_mutations=set(),
            )

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


if __name__ == "__main__":
    unittest.main()
