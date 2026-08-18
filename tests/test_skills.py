import tempfile
import unittest
from pathlib import Path

from engineering_process.contracts import ContractError
from engineering_process.skills import skill_digest, validate_skills


PROCESS_ROOT = Path(__file__).resolve().parent.parent


class SkillTests(unittest.TestCase):
    def test_repository_skills_are_portable(self):
        self.assertEqual(validate_skills(PROCESS_ROOT / ".agents" / "skills"), [])

    def test_digest_changes_with_instruction_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "sample-skill"
            skill.mkdir()
            path = skill / "SKILL.md"
            path.write_text(
                "---\n"
                "name: sample-skill\n"
                "description: Perform a sample task when requested.\n"
                "---\n\n"
                "# Sample\n\n"
                "Perform the task.\n",
                encoding="utf-8",
            )
            first = skill_digest(root)
            path.write_text(path.read_text(encoding="utf-8") + "\nVerify it.\n")

            self.assertNotEqual(first, skill_digest(root))

    def test_rejects_agent_specific_core_instruction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "sample-skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\n"
                "name: sample-skill\n"
                "description: Perform a sample task when requested.\n"
                "---\n\n"
                "# Sample\n\n"
                "Use spawn_agent to perform the task.\n",
                encoding="utf-8",
            )

            self.assertTrue(
                any("agent-specific" in issue for issue in validate_skills(root))
            )

    def test_digest_refuses_invalid_skills(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "Bad_Name"
            skill.mkdir()
            (skill / "SKILL.md").write_text("not a skill", encoding="utf-8")

            with self.assertRaises(ContractError):
                skill_digest(root)
