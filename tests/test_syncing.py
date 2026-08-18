import json
import tempfile
import unittest
from pathlib import Path

from engineering_process import VERSION
from engineering_process.contracts import ContractError
from engineering_process.distribution import distribution_digest
from engineering_process.syncing import sync_skills, synchronized_state
from engineering_process.contracts import read_json, validate_process_lock


PROCESS_ROOT = Path(__file__).resolve().parent.parent
CORE_SKILLS = (
    "define-change-contract",
    "evolve-process",
    "finish-change",
    "implement-change",
    "plan-change",
    "review-change",
    "run-change",
    "verify-change",
)


class SyncTests(unittest.TestCase):
    def prepare_project(self, root: Path) -> None:
        process = root / ".process"
        process.mkdir()
        digest = distribution_digest(PROCESS_ROOT, CORE_SKILLS)
        (process / "process.lock").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "process": {"version": VERSION, "digest": digest},
                    "skills": list(CORE_SKILLS),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def test_syncs_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            self.prepare_project(project_root)

            self.assertEqual(
                sync_skills(project_root, PROCESS_ROOT, check=False),
                [],
            )
            lock = validate_process_lock(
                read_json(project_root / ".process" / "process.lock")
            )
            self.assertEqual(
                synchronized_state(project_root, PROCESS_ROOT, lock),
                [],
            )

            target = (
                project_root
                / ".agents"
                / "skills"
                / "verify-change"
                / "SKILL.md"
            )
            target.write_text("tampered\n", encoding="utf-8")

            self.assertTrue(
                any(
                    "content differs" in issue
                    for issue in synchronized_state(project_root, PROCESS_ROOT, lock)
                )
            )

    def test_refuses_to_overwrite_unmanaged_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            self.prepare_project(project_root)
            target = project_root / ".agents" / "skills" / "verify-change"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("local\n", encoding="utf-8")

            with self.assertRaisesRegex(ContractError, "unmanaged"):
                sync_skills(project_root, PROCESS_ROOT, check=False)

    def test_detects_unmanaged_project_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            self.prepare_project(project_root)
            self.assertEqual([], sync_skills(project_root, PROCESS_ROOT, check=False))
            target = project_root / ".agents" / "skills" / "local-process"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("local\n", encoding="utf-8")
            lock = validate_process_lock(
                read_json(project_root / ".process" / "process.lock")
            )

            self.assertTrue(
                any(
                    "unmanaged project skill" in issue
                    for issue in synchronized_state(project_root, PROCESS_ROOT, lock)
                )
            )

            with self.assertRaisesRegex(ContractError, "unmanaged project skill"):
                sync_skills(project_root, PROCESS_ROOT, check=False)

    def test_detects_unmanaged_asset_at_skills_root(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            self.prepare_project(project_root)
            skills = project_root / ".agents" / "skills"
            skills.mkdir(parents=True)
            (skills / "README.md").write_text("local catalog\n", encoding="utf-8")
            lock = validate_process_lock(
                read_json(project_root / ".process" / "process.lock")
            )

            self.assertTrue(
                any(
                    "unmanaged project skill asset" in issue
                    for issue in synchronized_state(project_root, PROCESS_ROOT, lock)
                )
            )
