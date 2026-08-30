from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent.parent


class ArchitectureTests(unittest.TestCase):
    def test_runtime_stays_within_the_small_architecture_budget(self) -> None:
        modules = sorted((ROOT / "engineering_process").glob("*.py"))
        lines = sum(len(path.read_text(encoding="utf-8").splitlines()) for path in modules)
        self.assertLessEqual(len(modules), 18)
        self.assertLess(lines, 4000)

    def test_removed_governance_subsystems_do_not_return(self) -> None:
        removed_modules = {
            "artifact_attestation.py",
            "evidence_transport.py",
            "improvement.py",
            "publication.py",
            "recommendation.py",
            "remote_verification.py",
            "supplemental.py",
            "transition.py",
        }
        actual = {path.name for path in (ROOT / "engineering_process").glob("*.py")}
        self.assertTrue(removed_modules.isdisjoint(actual))
        workflows = {path.name for path in (ROOT / ".github" / "workflows").glob("*.yml")}
        self.assertEqual({"ci.yml", "publish.yml", "release-pr.yml"}, workflows)

    def test_source_skills_have_no_orphaned_legacy_directories(self) -> None:
        skills = {
            path.parent.name
            for path in (ROOT / "process_assets" / "skills").glob("*/SKILL.md")
        }
        self.assertEqual(8, len(skills))
        self.assertNotIn("publish-change", skills)
        self.assertNotIn("cross-repo-change", skills)


if __name__ == "__main__":
    unittest.main()
