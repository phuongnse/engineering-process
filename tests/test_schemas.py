import json
import unittest
from pathlib import Path

import jsonschema


PROCESS_ROOT = Path(__file__).resolve().parent.parent


class SchemaTests(unittest.TestCase):
    def test_all_schemas_are_valid_draft_2020_12(self):
        for path in sorted((PROCESS_ROOT / "schemas").glob("*.json")):
            with self.subTest(schema=path.name):
                document = json.loads(path.read_text(encoding="utf-8"))
                jsonschema.Draft202012Validator.check_schema(document)

    def test_packaged_examples_match_their_schemas(self):
        mappings = {
            "adoption-migration": "adoption-migration",
            "automation-policy": "automation-policy",
            "automation-proposal-policy": "automation-proposal-policy",
            "automation-proposal": "automation-proposal",
            "automation-process-adoption-policy": "automation-proposal-policy",
            "automation-process-adoption-proposal": "automation-proposal",
            "change": "change",
            "improvement-catalog": "improvement-catalog",
            "improvement-disposition": "improvement-disposition",
            "improvement-reproduction": "improvement-reproduction",
            "improvement-resolution": "improvement-resolution",
            "improvement-signal": "improvement-signal",
            "plan": "plan",
            "plan-decision-review": "plan-decision-review",
            "plan-decision-review-assignment": "plan-decision-review-assignment",
            "project": "project",
            "recommendation": "recommendation",
            "recommendation-resolution": "recommendation-resolution",
            "recommendation-review": "recommendation-review",
            "recommendation-review-assignment": "recommendation-review-assignment",
            "release": "release",
            "release-change": "release-change",
            "review": "review",
        }
        for example_name, schema_name in mappings.items():
            with self.subTest(example=example_name):
                example = json.loads(
                    (PROCESS_ROOT / "examples" / f"{example_name}.json").read_text(
                        encoding="utf-8"
                    )
                )
                schema = json.loads(
                    (PROCESS_ROOT / "schemas" / f"{schema_name}.schema.json").read_text(
                        encoding="utf-8"
                    )
                )
                jsonschema.Draft202012Validator(schema).validate(example)

    def test_process_graph_matches_its_schema(self):
        graph = json.loads(
            (PROCESS_ROOT / "process-graph.json").read_text(encoding="utf-8")
        )
        schema = json.loads(
            (PROCESS_ROOT / "schemas" / "process-graph.schema.json").read_text(
                encoding="utf-8"
            )
        )

        jsonschema.Draft202012Validator(schema).validate(graph)

    def test_historical_automation_proposal_schema_one_remains_valid(self):
        policy = json.loads(
            (PROCESS_ROOT / "examples" / "automation-proposal-policy.json").read_text(
                encoding="utf-8"
            )
        )
        policy["schemaVersion"] = 1
        policy["requiredControls"]["humanMergeRequired"] = True
        policy_schema = json.loads(
            (
                PROCESS_ROOT
                / "schemas"
                / "automation-proposal-policy.schema.json"
            ).read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator(policy_schema).validate(policy)

        proposal = json.loads(
            (PROCESS_ROOT / "examples" / "automation-proposal.json").read_text(
                encoding="utf-8"
            )
        )
        proposal["schemaVersion"] = 1
        proposal["observedControls"]["humanMergeRequired"] = True
        proposal["optIn"]["document"] = policy
        proposal_schema = json.loads(
            (PROCESS_ROOT / "schemas" / "automation-proposal.schema.json").read_text(
                encoding="utf-8"
            )
        )
        jsonschema.Draft202012Validator(proposal_schema).validate(proposal)

    def test_plan_cardinality_bounds_are_versioned(self):
        schema = json.loads(
            (PROCESS_ROOT / "schemas" / "plan.schema.json").read_text(
                encoding="utf-8"
            )
        )
        document = {
            "schemaVersion": 1,
            "changeId": "change-1",
            "contractDigest": f"sha256:{'0' * 64}",
            "approach": "Preserve the published schema while adding a bounded successor.",
            "workItems": [
                {
                    "id": f"work-{index}",
                    "outcome": "Implement the accepted behavior.",
                    "affectedPaths": ["src/"],
                    "verificationProfiles": ["development"],
                }
                for index in range(257)
            ],
            "acceptancePlan": [
                {
                    "criterionId": "ac-1",
                    "workItems": ["work-0"],
                    "verificationProfiles": ["development"],
                }
            ],
            "risks": [],
            "openDecisions": [],
        }
        validator = jsonschema.Draft202012Validator(schema)

        self.assertTrue(validator.is_valid(document))
        document["schemaVersion"] = 2
        self.assertFalse(validator.is_valid(document))

    def test_project_impact_is_additive_to_schema_three(self):
        schema = json.loads(
            (PROCESS_ROOT / "schemas" / "project.schema.json").read_text(
                encoding="utf-8"
            )
        )
        document = json.loads(
            (PROCESS_ROOT / "examples" / "project.json").read_text(
                encoding="utf-8"
            )
        )
        document["schemaVersion"] = 3
        validator = jsonschema.Draft202012Validator(schema)

        self.assertTrue(validator.is_valid(document))
        document["schemaVersion"] = 2
        document["environment"].pop("foregroundOnly")
        self.assertFalse(validator.is_valid(document))


if __name__ == "__main__":
    unittest.main()
