import argparse
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from engineering_process.cli import build_parser
from engineering_process.command_catalog import LIFECYCLE_COMMAND_PATHS
from engineering_process.contracts import ContractError
from engineering_process.process_graph import load_process_graph


PROCESS_ROOT = Path(__file__).resolve().parent.parent
SKILLS_ROOT = PROCESS_ROOT / "process_assets" / "skills"


def parser_command_paths(parser: argparse.ArgumentParser, prefix=()):
    paths = set()
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, child in action.choices.items():
            path = (*prefix, name)
            paths.add(" ".join(path))
            paths.update(parser_command_paths(child, path))
    return paths


class ProcessGraphTests(unittest.TestCase):
    def test_graph_declares_the_complete_pre_pr_review_chain(self):
        graph = load_process_graph(PROCESS_ROOT, SKILLS_ROOT)
        states = {state["id"]: state for state in graph["states"]}

        self.assertEqual("run-change", graph["entrySkill"])
        self.assertEqual("define-change-contract", states["unregistered"]["ownerSkill"])
        self.assertEqual("plan-change", states["specified"]["ownerSkill"])
        self.assertEqual("implement-change", states["planned"]["ownerSkill"])
        self.assertEqual("verify-change", states["implementing"]["ownerSkill"])
        self.assertEqual("review-change", states["verified"]["ownerSkill"])
        self.assertEqual("review-change", states["review-pending"]["ownerSkill"])
        self.assertEqual("implement-change", states["changes-requested"]["ownerSkill"])
        self.assertEqual("finish-change", states["approved"]["ownerSkill"])
        self.assertEqual("publish-change", states["completed"]["ownerSkill"])
        self.assertEqual("human", states["awaiting-human-merge"]["actor"])
        self.assertIsNone(states["awaiting-human-merge"]["ownerSkill"])
        self.assertEqual(
            ["completed"],
            [
                state["id"]
                for state in graph["states"]
                if state["id"] != "awaiting-human-merge" and any(
                    transition["nextState"] == "awaiting-human-merge"
                    for transition in state["transitions"]
                )
            ],
        )

    def test_graph_commands_exist_in_the_actual_cli(self):
        available = parser_command_paths(build_parser())
        self.assertTrue(LIFECYCLE_COMMAND_PATHS <= available)

    def test_graph_rejects_command_and_skill_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(SKILLS_ROOT, root / "process_assets" / "skills")
            shutil.copy2(PROCESS_ROOT / "bundles.json", root / "bundles.json")
            graph = json.loads(
                (PROCESS_ROOT / "process-graph.json").read_text(encoding="utf-8")
            )
            graph["states"][0]["commands"] = ["change nonexistent"]
            (root / "process-graph.json").write_text(
                json.dumps(graph), encoding="utf-8"
            )

            with self.assertRaisesRegex(ContractError, "unknown processctl commands"):
                load_process_graph(root, root / "process_assets" / "skills")

            graph["states"][0]["commands"] = ["change start"]
            graph["states"][0]["ownerSkill"] = "missing-skill"
            (root / "process-graph.json").write_text(
                json.dumps(graph), encoding="utf-8"
            )
            with self.assertRaisesRegex(ContractError, "unknown skill"):
                load_process_graph(root, root / "process_assets" / "skills")


if __name__ == "__main__":
    unittest.main()
