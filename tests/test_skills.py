from __future__ import annotations

from pathlib import Path
import unittest

from engineering_process.contracts import read_json
from engineering_process.skills import validate_skills


ROOT = Path(__file__).resolve().parent.parent


class SkillTests(unittest.TestCase):
    def test_distribution_has_one_reachable_eight_skill_graph(self) -> None:
        result = validate_skills(ROOT / "process_assets" / "skills", process_root=ROOT)
        self.assertEqual(8, result["count"])
        self.assertEqual("run-change", result["entrySkill"])
        self.assertEqual(
            {
                "finish-change",
                "implement-change",
                "improve-process",
                "plan-change",
                "review-change",
                "run-change",
                "start-change",
                "verify-change",
            },
            set(result["skills"]),
        )

    def test_graph_exposes_only_the_six_phase_cli(self) -> None:
        graph = read_json(ROOT / "process-graph.json")
        commands = {
            command for state in graph["states"] for command in state["commands"]
        }
        self.assertEqual(
            {
                "change finish",
                "change implement",
                "change plan",
                "change review start",
                "change review submit",
                "change start",
                "change verify",
            },
            commands,
        )
        self.assertNotIn("improvement-required", {state["id"] for state in graph["states"]})

    def test_existing_skill_graph_guides_readiness_without_global_blocking(self) -> None:
        roots = ROOT / "process_assets" / "skills"
        required = {
            "run-change": ("processctl project validate", "never choose the\nproduct roadmap autonomously"),
            "start-change": ("affected\nenforced capability", "Unrelated planned gaps remain visible and non-blocking"),
            "plan-change": ("do not add unrelated planned gaps", "checklist edit alone is not evidence"),
            "implement-change": ("Never auto-promote", "do not work unrelated planned\ngaps"),
            "verify-change": ("do not run every planned production gate", "same snapshot"),
            "review-change": ("Do not block the change merely because unrelated planned capabilities", "reject promotion by prose"),
            "finish-change": ("Finish never edits readiness", "remaining planned\ngaps"),
            "improve-process": ("new immutable pack version", "never\nself-publishes or self-merges"),
        }
        for skill, fragments in required.items():
            text = (roots / skill / "SKILL.md").read_text(encoding="utf-8")
            for fragment in fragments:
                with self.subTest(skill=skill, fragment=fragment):
                    self.assertIn(fragment, text)

    def test_improve_process_supports_owner_authorized_issue_handoff(self) -> None:
        text = (ROOT / "process_assets" / "skills" / "improve-process" / "SKILL.md").read_text(encoding="utf-8")
        for fragment in (
            "fix or safely block the current consumer change",
            "gh issue list --repo phuongnse/engineering-process",
            "Search before creating",
            "only after explicit authorization",
            "gh issue create --repo phuongnse/engineering-process",
            "Do not run issue creation from consumer CI",
            "open-issue search URL containing the complete stable key",
            "must search and reuse an\nexisting issue before manual submission",
            "use its URL as the process change `source`",
            "Close the issue only after",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)


if __name__ == "__main__":
    unittest.main()
