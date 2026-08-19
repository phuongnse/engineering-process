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
            "change": "change",
            "plan": "plan",
            "project": "project",
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


if __name__ == "__main__":
    unittest.main()
