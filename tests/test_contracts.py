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

    def test_review_v6_requires_durable_non_blocking_dispositions(self) -> None:
        review = {
            "schemaVersion": 6,
            "changeId": "sample-change",
            "reviewer": {
                "actorId": "reviewer",
                "contextId": "review-context",
                "kind": "agent",
            },
            "checkpoint": {
                "head": "0" * 40,
                "fingerprint": f"sha256:{'0' * 64}",
                "fileCount": 1,
                "byteCount": 1,
            },
            "verdict": "approved",
            "summary": "Reviewed the accepted snapshot.",
            "findings": [
                {
                    "id": "follow-up",
                    "severity": "non-blocking",
                    "priority": "P3",
                    "criterionId": "works",
                    "origin": "contract",
                    "summary": "A bounded follow-up remains.",
                }
            ],
        }
        with self.assertRaisesRegex(ProcessError, "disposition"):
            validate_document(review, "review", schema_root=SCHEMAS)

        legacy = deepcopy(review)
        legacy["schemaVersion"] = 5
        validate_document(legacy, "review", schema_root=SCHEMAS)

        resolved = deepcopy(review)
        resolved["findings"][0]["disposition"] = {
            "status": "resolved",
            "rationale": "Resolved in the reviewed snapshot.",
        }
        validate_document(resolved, "review", schema_root=SCHEMAS)

        for status in ("accepted-risk", "tracked-follow-up"):
            durable = deepcopy(review)
            durable["findings"][0]["disposition"] = {
                "status": status,
                "rationale": "The accepted behavior remains complete.",
                "owner": "process-owner",
                "recordUrl": "https://github.com/phuongnse/engineering-process/issues/111",
            }
            with self.subTest(status=status):
                validate_document(durable, "review", schema_root=SCHEMAS)
            durable["findings"][0]["disposition"].pop("owner")
            with self.subTest(status=status, missing="owner"):
                with self.assertRaisesRegex(ProcessError, "owner"):
                    validate_document(durable, "review", schema_root=SCHEMAS)
            durable["findings"][0]["disposition"]["owner"] = "process-owner"
            durable["findings"][0]["disposition"]["recordUrl"] = "http://example.invalid/111"
            with self.subTest(status=status, invalid="recordUrl"):
                with self.assertRaisesRegex(ProcessError, "recordUrl"):
                    validate_document(durable, "review", schema_root=SCHEMAS)

    def test_released_process_lock_schema_uri_remains_accepted(self) -> None:
        lock = read_json(ROOT / ".process" / "process.lock")
        lock["$schema"] = "https://engineering-process.invalid/schemas/process-lock.schema.json"
        validate_document(lock, "process-lock", schema_root=SCHEMAS)

    def test_live_readiness_resolves_library_cli_coverage(self) -> None:
        project = load_project(ROOT, ROOT)
        readiness = readiness_summary(project)
        self.assertEqual([{"id": "library-cli", "version": 1}], readiness["packs"])
        self.assertEqual("production", readiness["stage"])
        self.assertEqual([], readiness["plannedCapabilities"])
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
            {"id": "correctness", "state": "enforced", "evidenceProfiles": ["review"]}
        )
        cases.append((duplicate, "ids must be unique"))
        unknown = deepcopy(live)
        unknown["readiness"]["capabilities"][0]["evidenceProfiles"] = ["missing"]
        cases.append((unknown, "unknown profiles"))
        optional = deepcopy(live)
        optional["profiles"]["security"] = deepcopy(optional["profiles"]["review"])
        optional["readiness"]["capabilities"][0]["evidenceProfiles"] = ["security"]
        cases.append((optional, "optional profiles"))
        deadlocked = deepcopy(live)
        deadlocked["lifecycle"]["requiredProfiles"].append("missing")
        cases.append((deadlocked, "project requires unknown profiles"))
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
                "stage": "production",
                "packs": [{"id": "operations", "version": 1}],
                "capabilities": [
                    {"id": capability, "state": "enforced", "evidenceProfiles": profiles}
                    for capability, profiles in evidence.items()
                ],
            },
        }
        readiness = readiness_summary(normalize_project(project, ROOT))
        self.assertEqual(set(evidence), set(readiness["capabilities"]))
        project["readiness"]["capabilities"].pop()
        with self.assertRaisesRegex(ProcessError, "missing capabilities"):
            normalize_project(project, ROOT)

    def test_pack_versions_are_explicit_and_do_not_upgrade_implicitly(self) -> None:
        project = load_project(ROOT, ROOT)
        project["readiness"]["packs"] = [{"id": "library-cli", "version": 2}]
        with self.assertRaisesRegex(ProcessError, "unsupported readiness pack versions: library-cli@2"):
            readiness_summary(project)

    def test_desktop_media_tracks_enforced_evidence_and_planned_gaps(self) -> None:
        checks = {
            "frontend": ["frontend-build", "frontend-tests", "frontend-dependency-audit"],
            "python": ["python-compile", "python-dependency-consistency", "python-tests", "python-media-integration"],
            "rust": ["rust-format", "rust-tests", "rust-clippy"],
            "security": ["python-dependency-audit", "rust-dependency-audit", "package-fuzz-smoke"],
        }
        evidence = {
            "application-correctness": ["frontend", "python", "rust"],
            "authoritative-input-integrity": ["python"],
            "cross-platform-portability": ["frontend", "python", "rust"],
            "dependency-audit": ["frontend", "security"],
            "media-pipeline-integrity": ["python"],
            "package-security": ["rust", "security"],
            "recovery-mechanism-integrity": ["python", "rust"],
        }
        gaps = {
            "dependency-security": "Linux GTK/glib advisories and audit coverage exclusions remain stable-release blockers.",
            "incident-recovery": "Signing-key compromise and destructive recovery drills remain open.",
            "independent-security-review": "The format, key lifecycle, parser, player, runtime, broker, and update chain still require independent assessment.",
            "key-custody": "The release signing seed still needs documented offline or hardware-backed custody.",
            "linux-release-security": "The Tauri GTK3/glib unsoundness and unmaintained dependency chain remains unresolved.",
            "recovery-integrity": "Recovery must be verified before the last clear master can be removed.",
            "release-integrity": "Signed installers and clean-host platform release evidence remain open.",
            "runtime-delivery-integrity": "Runtime delivery and model/checkpoint redistribution licensing remain unresolved.",
            "update-integrity": "A signed updater and rollback policy remain open.",
            "workspace-security": "Credential storage and encrypted-workspace adapters still need real-host evidence.",
        }
        project = {
            "schemaVersion": 5,
            "project": "lyric-rail",
            "lifecycle": {"requiredProfiles": ["frontend", "python", "rust"]},
            "profiles": {
                profile: [
                    {"id": check, "run": ["command"], "timeoutSeconds": 300}
                    for check in identities
                ]
                for profile, identities in checks.items()
            },
            "readiness": {
                "target": "production",
                "stage": "building",
                "packs": [{"id": "desktop-media", "version": 1}],
                "capabilities": [
                    *(
                        {"id": capability, "state": "enforced", "evidenceProfiles": profiles}
                        for capability, profiles in evidence.items()
                    ),
                    *(
                        {"id": capability, "state": "planned", "gap": gap}
                        for capability, gap in gaps.items()
                    ),
                ],
            },
        }
        summary = readiness_summary(normalize_project(project, ROOT))
        self.assertEqual("building", summary["stage"])
        self.assertEqual(set(gaps), set(summary["plannedCapabilities"]))
        self.assertEqual("enforced", summary["capabilities"]["package-security"]["state"])
        project["readiness"]["stage"] = "production"
        with self.assertRaisesRegex(ProcessError, "production readiness cannot contain planned capabilities"):
            normalize_project(project, ROOT)
        project["readiness"]["stage"] = "building"
        next(item for item in project["readiness"]["capabilities"] if item["state"] == "planned")["gap"] = " "
        with self.assertRaises(ProcessError):
            normalize_project(project, ROOT)


if __name__ == "__main__":
    unittest.main()
