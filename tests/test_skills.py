from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import unittest
from unittest import mock

from engineering_process.contracts import ProcessError, read_json
from engineering_process.skills import validate_skills


ROOT = Path(__file__).resolve().parent.parent


class SkillTests(unittest.TestCase):
    def test_distribution_has_one_complete_reachable_skill_graph(self) -> None:
        result = validate_skills(ROOT / "process_assets" / "skills", process_root=ROOT)
        self.assertEqual("deliver-change", result["entrySkill"])
        expected = {
            "change-complete",
            "change-implement",
            "process-improve",
            "change-plan",
            "production-engineering",
            "change-review",
            "deliver-change",
            "change-start",
            "change-verify",
        }
        self.assertEqual(expected, set(result["skills"]))
        self.assertEqual(len(expected), result["count"])

    def test_graph_rejects_missing_and_partial_transition_targets(self) -> None:
        graph = read_json(ROOT / "process-graph.json")
        for destination, skill in (
            ("specified", "missing-skill"),
            ("missing-state", "change-plan"),
            (None, "change-plan"),
            ("specified", None),
        ):
            invalid = deepcopy(graph)
            start = next(state for state in invalid["states"] if state["id"] == "unregistered")
            start["transitions"][0].update(nextState=destination, nextSkill=skill)
            with self.subTest(destination=destination, skill=skill), mock.patch(
                "engineering_process.skills.load_and_validate", return_value=invalid
            ), self.assertRaisesRegex(ProcessError, "process graph"):
                validate_skills(ROOT / "process_assets" / "skills", process_root=ROOT)

    def test_changed_source_routes_through_implementation_before_verification(self) -> None:
        graph = read_json(ROOT / "process-graph.json")
        for state in graph["states"]:
            if state["id"] in {"verified", "review-pending", "approved"}:
                transition = next(item for item in state["transitions"] if item["result"] == "source-changed")
                with self.subTest(state=state["id"]):
                    self.assertEqual("implementing", transition["nextState"])
                    self.assertEqual("change-implement", transition["nextSkill"])
        validate_skills(ROOT / "process_assets" / "skills", process_root=ROOT)

    def test_graph_rejects_ambiguous_state_owners(self) -> None:
        graph = read_json(ROOT / "process-graph.json")
        graph["states"][-1]["id"] = "specified"
        with mock.patch(
            "engineering_process.skills.load_and_validate", return_value=graph
        ), self.assertRaisesRegex(ProcessError, "state ids must be unique"):
            validate_skills(ROOT / "process_assets" / "skills", process_root=ROOT)

    def test_readme_skill_links_resolve(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        links = re.findall(r"\]\((process_assets/skills/[^)#]+)(?:#[^)]*)?\)", readme)
        self.assertTrue(links)
        for relative in links:
            with self.subTest(relative=relative):
                self.assertTrue((ROOT / relative).is_file())

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
            "deliver-change": ("processctl project validate", "never choose the\nproduct roadmap autonomously"),
            "change-start": ("affected\nenforced capability", "Unrelated planned gaps remain visible and non-blocking"),
            "change-plan": ("do not add unrelated planned gaps", "checklist edit alone is not evidence"),
            "change-implement": (
                "Never auto-promote",
                "do not work unrelated planned\ngaps",
                "production-engineering",
            ),
            "production-engineering": (
                "small correctness floor",
                "not a design-pattern catalog",
                "open-world meaning",
                "Approval is impossible",
                "readiness declaration",
            ),
            "change-verify": ("do not run every planned production gate", "same snapshot"),
            "change-review": (
                "Do not block the change merely because unrelated planned capabilities",
                "Priority records impact if the finding remains unresolved",
                "never omit an\nobservation merely to reach approval",
                "stable\nHTTPS `recordUrl`",
                "bounded `processSignals`",
                "keep the assignment `review-pending`",
            ),
            "change-complete": (
                "Finish never edits\nreadiness",
                "remaining planned\ngaps",
                "owner and stable record URL",
                "contract-identified final consumer adoption",
                "Closes ISSUE, closes ISSUE",
            ),
            "process-improve": (
                "new immutable pack version",
                "pending schema-version 7 review",
                "never\nself-publishes or self-merges",
            ),
        }
        for skill, fragments in required.items():
            text = (roots / skill / "SKILL.md").read_text(encoding="utf-8")
            for fragment in fragments:
                with self.subTest(skill=skill, fragment=fragment):
                    self.assertIn(" ".join(fragment.split()), " ".join(text.split()))

    def test_improve_process_supports_owner_authorized_issue_handoff(self) -> None:
        text = (ROOT / "process_assets" / "skills" / "process-improve" / "SKILL.md").read_text(encoding="utf-8")
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
            "a `shared-process` disposition cannot submit without that\n`recordUrl`",
            "Close the issue only after",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)


if __name__ == "__main__":
    unittest.main()
