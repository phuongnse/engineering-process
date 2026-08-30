from __future__ import annotations

from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from engineering_process.contracts import ProcessError, read_json, validate_document
from engineering_process.distribution import schemas_root
from engineering_process.project import load_project, normalize_project, require_consumer_evidence


ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = schemas_root(ROOT)


class ContractTests(unittest.TestCase):
    def test_every_schema_is_valid_and_is_used(self) -> None:
        expected = {
            "change",
            "plan",
            "process-graph",
            "process-lock",
            "project",
            "project-legacy",
            "receipt",
            "release-change",
            "release",
            "review",
            "run",
        }
        actual = {path.name.removesuffix(".schema.json") for path in SCHEMAS.glob("*.json")}
        self.assertEqual(expected, actual)
        for path in SCHEMAS.glob("*.json"):
            Draft202012Validator.check_schema(read_json(path))

    def test_live_repository_contracts_validate(self) -> None:
        cases = [
            (ROOT / ".process" / "process.lock", "process-lock"),
            (ROOT / "process-graph.json", "process-graph"),
            (ROOT / "release.json", "release"),
        ]
        cases.extend(
            (path, "release-change")
            for path in sorted((ROOT / "release-changes").glob("*.json"))
        )
        for path, kind in cases:
            with self.subTest(path=path):
                validate_document(read_json(path), kind, schema_root=SCHEMAS, source=str(path))
        normalized = normalize_project(read_json(ROOT / ".process" / "project.json"), ROOT)
        validate_document(normalized, "project", schema_root=SCHEMAS)
        self.assertTrue(require_consumer_evidence(load_project(ROOT, ROOT)))

    def test_schema_rejects_unknown_fields(self) -> None:
        project = normalize_project(read_json(ROOT / ".process" / "project.json"), ROOT)
        project["governanceLayer"] = True
        with self.assertRaisesRegex(ProcessError, "Additional properties"):
            validate_document(project, "project", schema_root=SCHEMAS)

    def test_released_process_lock_schema_uri_remains_accepted(self) -> None:
        lock = read_json(ROOT / ".process" / "process.lock")
        lock["$schema"] = "https://engineering-process.invalid/schemas/process-lock.schema.json"
        validate_document(lock, "process-lock", schema_root=SCHEMAS)


if __name__ == "__main__":
    unittest.main()
