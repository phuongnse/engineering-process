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


if __name__ == "__main__":
    unittest.main()
