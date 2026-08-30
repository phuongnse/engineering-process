from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from engineering_process.contracts import ProcessError, read_json, validate_document
from engineering_process.distribution import schemas_root
from engineering_process.project import (
    load_project,
    normalize_project,
    readiness_summary,
    require_consumer_evidence,
)


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

    def test_live_readiness_resolves_library_cli_coverage(self) -> None:
        project = load_project(ROOT, ROOT)
        readiness = readiness_summary(project)
        self.assertEqual(["library-cli"], readiness["packs"])
        self.assertEqual(
            {
                "adoption-integrity",
                "compatibility",
                "correctness",
                "distribution-integrity",
                "installability",
                "portability",
                "runtime-safety",
            },
            set(readiness["capabilities"]),
        )

    def test_readiness_fails_closed_on_incomplete_or_ambiguous_evidence(self) -> None:
        live = read_json(ROOT / ".process" / "project.json")
        live["readiness"] = read_json(ROOT / ".process" / "readiness.json")
        cases = []
        missing = deepcopy(live)
        missing["readiness"]["capabilities"].pop()
        cases.append((missing, "missing capabilities"))
        duplicate = deepcopy(live)
        duplicate["readiness"]["capabilities"].append(
            {"id": "correctness", "evidenceProfiles": ["review"]}
        )
        cases.append((duplicate, "ids must be unique"))
        unknown = deepcopy(live)
        unknown["readiness"]["capabilities"][0]["evidenceProfiles"] = ["missing"]
        cases.append((unknown, "unknown profiles"))
        optional = deepcopy(live)
        optional["profiles"]["security"] = deepcopy(optional["profiles"]["review"])
        optional["readiness"]["capabilities"][0]["evidenceProfiles"] = ["security"]
        cases.append((optional, "optional profiles"))
        for project, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ProcessError, message):
                normalize_project(project, ROOT)

    def test_readiness_remains_optional_for_existing_schema_v5_consumers(self) -> None:
        project = read_json(ROOT / ".process" / "project.json")
        self.assertIsNone(readiness_summary(normalize_project(project, ROOT)))

    def test_operations_pack_resolves_renovate_ops_profiles_and_fails_closed(self) -> None:
        evidence = {
            "auditability": ["development"],
            "automation-correctness": ["development"],
            "bounded-execution": ["development"],
            "least-privilege": ["development", "review"],
            "policy-integrity": ["development", "review"],
            "recovery": ["development"],
            "target-selection-integrity": ["development"],
        }
        project = {
            "schemaVersion": 5,
            "project": "renovate-ops",
            "lifecycle": {"requiredProfiles": ["development", "review"]},
            "profiles": {
                "development": [{"id": "unit", "run": ["node", "--test"], "timeoutSeconds": 900}],
                "review": [
                    {"id": "global-renovate-config", "run": ["renovate-config-validator", "config.cjs", "--strict"], "timeoutSeconds": 300},
                    {"id": "repository-renovate-config", "run": ["renovate-config-validator", ".github/renovate.json5", "--strict"], "timeoutSeconds": 300},
                ],
            },
            "readiness": {
                "target": "production",
                "packs": ["operations"],
                "capabilities": [
                    {"id": capability, "evidenceProfiles": profiles}
                    for capability, profiles in evidence.items()
                ],
            },
        }
        readiness = readiness_summary(normalize_project(project, ROOT))
        self.assertEqual(set(evidence), set(readiness["capabilities"]))
        project["readiness"]["capabilities"].pop()
        with self.assertRaisesRegex(ProcessError, "missing capabilities"):
            normalize_project(project, ROOT)


if __name__ == "__main__":
    unittest.main()
