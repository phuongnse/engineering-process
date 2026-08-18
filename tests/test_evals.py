import unittest
from pathlib import Path

from engineering_process.contracts import read_json
from engineering_process.skills import skill_directories


PROCESS_ROOT = Path(__file__).resolve().parent.parent


class EvaluationFixtureTests(unittest.TestCase):
    def test_every_skill_has_one_portable_forward_case(self):
        path = PROCESS_ROOT / "evals" / "cases.json"
        document = read_json(path)

        self.assertEqual(set(document), {"schemaVersion", "cases"})
        self.assertEqual(document["schemaVersion"], 1)
        skills = {
            directory.name
            for directory in skill_directories(
                PROCESS_ROOT / ".agents" / "skills"
            )
        }
        case_skills = []
        case_ids = []
        for case in document["cases"]:
            self.assertEqual(
                set(case),
                {"id", "skill", "prompt", "mustInclude", "mustNotInclude"},
            )
            case_ids.append(case["id"])
            case_skills.append(case["skill"])
            self.assertTrue(case["prompt"])
            self.assertTrue(case["mustInclude"])
            self.assertTrue(case["mustNotInclude"])

        self.assertEqual(len(case_ids), len(set(case_ids)))
        self.assertEqual(set(case_skills), skills)
        self.assertEqual(len(case_skills), len(set(case_skills)))
