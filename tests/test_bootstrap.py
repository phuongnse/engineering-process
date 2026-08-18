import json
import tempfile
import unittest
from pathlib import Path

from engineering_process.bootstrap import AGENTS_END, AGENTS_START, initialize_project
from engineering_process.contracts import ContractError, read_json


PROCESS_ROOT = Path(__file__).resolve().parent.parent


class BootstrapTests(unittest.TestCase):
    def write_manifest(self, root: Path, project: str = "sample") -> Path:
        path = root / "source-project.json"
        path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "project": project,
                    "lifecycle": {"requiredProfiles": ["development", "review"]},
                    "profiles": {
                        "development": [
                            {
                                "id": "unit",
                                "run": ["python", "-c", "raise SystemExit(0)"],
                                "timeoutSeconds": 30,
                            }
                        ],
                        "review": [
                            {
                                "id": "review",
                                "run": ["python", "-c", "raise SystemExit(0)"],
                                "timeoutSeconds": 30,
                            }
                        ],
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_initializes_and_updates_managed_contract_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("# Product rules\n", encoding="utf-8")
            (root / ".gitignore").write_text("dist/\n.process/runs/\n", encoding="utf-8")
            manifest = self.write_manifest(root)
            first = initialize_project(
                root,
                PROCESS_ROOT,
                manifest_path=manifest,
                requested_bundles=["core", "delivery"],
                replace=False,
            )
            second = initialize_project(
                root,
                PROCESS_ROOT,
                manifest_path=manifest,
                requested_bundles=["core", "delivery"],
                replace=False,
            )

            self.assertEqual(first, second)
            agents = (root / "AGENTS.md").read_text(encoding="utf-8")
            self.assertEqual(agents.count(AGENTS_START), 1)
            self.assertEqual(agents.count(AGENTS_END), 1)
            self.assertIn("# Product rules", agents)
            ignore = (root / ".gitignore").read_text()
            self.assertIn("/.process/runs/", ignore)
            self.assertNotIn("\n.process/runs/", ignore)
            self.assertEqual(ignore.count("/.process/runs/"), 1)
            lock = read_json(root / ".process" / "process.lock")
            self.assertIn("assess-design", lock["skills"])
            self.assertTrue(
                (root / ".agents" / "skills" / "run-change" / "SKILL.md").is_file()
            )

    def test_refuses_to_replace_a_different_project_manifest_implicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.write_manifest(root, "first")
            initialize_project(
                root,
                PROCESS_ROOT,
                manifest_path=first,
                requested_bundles=["core"],
                replace=False,
            )
            second = self.write_manifest(root, "second")

            with self.assertRaisesRegex(ContractError, "use --replace"):
                initialize_project(
                    root,
                    PROCESS_ROOT,
                    manifest_path=second,
                    requested_bundles=["core"],
                    replace=False,
                )


if __name__ == "__main__":
    unittest.main()
