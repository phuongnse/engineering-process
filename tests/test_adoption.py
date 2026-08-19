import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engineering_process import VERSION
from engineering_process.adoption import (
    apply_adoption,
    check_adoption,
    validate_requirements_lock,
)
from engineering_process.bootstrap import initialize_project
from engineering_process.contracts import ContractError, read_json


PROCESS_ROOT = Path(__file__).resolve().parent.parent


class AdoptionTests(unittest.TestCase):
    def prepare_project(self, root: Path) -> Path:
        manifest = root / "project.json"
        manifest.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "project": "consumer",
                    "lifecycle": {"requiredProfiles": ["development"]},
                    "profiles": {
                        "development": [
                            {
                                "id": "unit",
                                "run": ["python", "-c", "raise SystemExit(0)"],
                                "timeoutSeconds": 30,
                            }
                        ]
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        initialize_project(
            root,
            PROCESS_ROOT,
            manifest_path=manifest,
            requested_bundles=["docs"],
            replace=False,
        )
        requirements = root / "requirements" / "process.txt"
        requirements.parent.mkdir()
        requirements.write_bytes(
            (PROCESS_ROOT / "requirements" / "process.txt").read_bytes()
        )
        return requirements

    def test_requirements_lock_binds_exact_hashed_public_authority(self):
        lock = validate_requirements_lock(
            PROCESS_ROOT / "requirements" / "process.txt"
        )

        authority = next(
            pin for pin in lock.pins if pin.name == "engineering-process"
        )
        self.assertEqual(VERSION, authority.version)
        self.assertTrue(authority.hashes)
        self.assertRegex(lock.digest, r"^sha256:[0-9a-f]{64}$")

    def test_apply_updates_lock_and_all_managed_assets_as_one_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requirements = self.prepare_project(root)
            lock_path = root / ".process" / "process.lock"
            previous = read_json(lock_path)
            previous["process"]["version"] = "0.1.0"
            lock_path.write_text(
                json.dumps(previous, indent=2) + "\n", encoding="utf-8"
            )
            managed = root / ".process" / "adopt-process.py"
            managed.write_text(
                managed.read_text(encoding="utf-8").replace(
                    "COMMAND_TIMEOUT_SECONDS = 300",
                    "COMMAND_TIMEOUT_SECONDS = 299",
                ),
                encoding="utf-8",
            )

            result = apply_adoption(root, PROCESS_ROOT, requirements)

            updated = read_json(lock_path)
            self.assertEqual("0.1.0", result["previousVersion"])
            self.assertEqual(VERSION, updated["process"]["version"])
            self.assertIn("maintain-docs", updated["skills"])
            self.assertEqual(
                (PROCESS_ROOT / "templates" / "adopt-process.py").read_bytes(),
                managed.read_bytes(),
            )
            self.assertEqual(
                (
                    PROCESS_ROOT
                    / "templates"
                    / "adopt-process-windows-job.py"
                ).read_bytes(),
                (
                    root / ".process" / "adopt-process-windows-job.py"
                ).read_bytes(),
            )
            self.assertEqual(
                [],
                check_adoption(root, PROCESS_ROOT, requirements)["issues"],
            )

    def test_apply_is_idempotent_and_preserves_selected_optional_skills(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requirements = self.prepare_project(root)

            first = apply_adoption(root, PROCESS_ROOT, requirements)
            first_lock = (root / ".process" / "process.lock").read_bytes()
            second = apply_adoption(root, PROCESS_ROOT, requirements)

            self.assertEqual(first_lock, (root / ".process" / "process.lock").read_bytes())
            self.assertEqual(first["digest"], second["digest"])
            self.assertIn("maintain-docs", second["skills"])

    def test_apply_rolls_back_every_managed_target_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requirements = self.prepare_project(root)
            lock = root / ".process" / "process.lock"
            agents = root / "AGENTS.md"
            runner = root / ".process" / "adopt-process.py"
            windows_helper = (
                root / ".process" / "adopt-process-windows-job.py"
            )
            before = {
                path: path.read_bytes()
                for path in (lock, agents, runner, windows_helper)
            }

            def fail_after_partial_write(*args, **kwargs):
                agents.write_text("partial\n", encoding="utf-8")
                runner.write_text("partial\n", encoding="utf-8")
                windows_helper.write_text("partial\n", encoding="utf-8")
                raise ContractError("injected adoption failure")

            with (
                mock.patch(
                    "engineering_process.adoption.sync_skills",
                    side_effect=fail_after_partial_write,
                ),
                self.assertRaisesRegex(ContractError, "injected adoption failure"),
            ):
                apply_adoption(root, PROCESS_ROOT, requirements)

            self.assertEqual(
                before,
                {
                    path: path.read_bytes()
                    for path in (lock, agents, runner, windows_helper)
                },
            )

    def test_requirements_lock_rejects_unhashed_or_mismatched_authority(self):
        cases = (
            "--only-binary :all:\nengineering-process==0.1.1\n",
            (
                "--only-binary :all:\nengineering-process==9.9.9 \\\n"
                f"    --hash=sha256:{'0' * 64}\n"
            ),
            (
                "--only-binary :all:\nhttps://example.invalid/process.whl \\\n"
                f"    --hash=sha256:{'0' * 64}\n"
            ),
        )
        for content in cases:
            with self.subTest(content=content.splitlines()[-1]):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "process.txt"
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(ContractError):
                        validate_requirements_lock(path)

    def test_apply_rejects_checkout_as_its_own_adoption_authority(self):
        with self.assertRaisesRegex(ContractError, "installed outside"):
            apply_adoption(
                PROCESS_ROOT,
                PROCESS_ROOT,
                PROCESS_ROOT / "requirements" / "process.txt",
            )

    def test_apply_rejects_an_arbitrary_external_process_root(self):
        with tempfile.TemporaryDirectory() as directory:
            external = Path(directory) / "untrusted-authority"
            with self.assertRaisesRegex(ContractError, "active installed process root"):
                apply_adoption(
                    PROCESS_ROOT,
                    external,
                    PROCESS_ROOT / "requirements" / "process.txt",
                )


if __name__ == "__main__":
    unittest.main()
