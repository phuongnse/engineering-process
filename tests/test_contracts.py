import unittest

from engineering_process.contracts import (
    CORE_QUALITY_DIMENSIONS,
    ContractError,
    validate_change,
    validate_plan,
    validate_process_lock,
    validate_project,
    validate_release,
    validate_review,
)


class ProjectContractTests(unittest.TestCase):
    def valid_project(self):
        return {
            "schemaVersion": 4,
            "project": "sample-project",
            "lifecycle": {"requiredProfiles": ["development"]},
            "profiles": {
                "development": [
                    {
                        "id": "unit",
                        "run": ["python", "-m", "unittest"],
                        "timeoutSeconds": 60,
                    }
                ]
            },
            "environment": {
                "defaultProfile": "development",
                "foregroundOnly": True,
                "managedTools": [],
                "profiles": {"development": ["python-runtime"]},
                "requirements": [
                    {
                        "id": "python-runtime",
                        "description": "Supported Python runtime",
                        "probe": {
                            "run": ["python", "--version"],
                            "timeoutSeconds": 15,
                            "readOnly": True,
                        },
                        "remediation": "Install a supported Python runtime.",
                    }
                ],
                "setupActions": [],
            },
        }

    def test_accepts_argument_array_checks(self):
        project = validate_project(self.valid_project())

        self.assertEqual(project.identifier, "sample-project")
        self.assertEqual(project.profiles["development"][0].run[0], "python")

    def test_accepts_schema_four_impact_graph(self):
        document = self.valid_project()
        document["impact"] = {
            "baseRefs": ["origin/main", "main"],
            "unmatchedPaths": "all-scoped-checks",
            "components": [
                {
                    "id": "api",
                    "paths": ["openapi.json", "src/api/**"],
                    "affects": ["frontend"],
                },
                {
                    "id": "frontend",
                    "paths": ["frontend/**"],
                    "affects": [],
                },
            ],
        }
        document["profiles"]["development"][0]["components"] = ["frontend"]

        project = validate_project(document)

        self.assertEqual(project.impact.base_refs, ("origin/main", "main"))
        self.assertEqual(
            project.profiles["development"][0].components,
            ("frontend",),
        )
        self.assertEqual(project.impact.components["api"].affects, ("frontend",))

    def test_impact_contract_is_additive_to_schema_three_but_not_older_majors(self):
        document = self.valid_project()
        document["schemaVersion"] = 3
        document["impact"] = {
            "baseRefs": ["main"],
            "unmatchedPaths": "all-scoped-checks",
            "components": [
                {"id": "source", "paths": ["src/**"], "affects": []}
            ],
        }
        document["lifecycle"]["qualityExtensions"] = ["project-accessibility"]
        document["profiles"]["development"][0]["components"] = ["source"]

        project = validate_project(document)

        self.assertEqual(("source",), project.profiles["development"][0].components)
        self.assertEqual(("project-accessibility",), project.quality_extensions)

        document["schemaVersion"] = 2
        del document["environment"]["foregroundOnly"]

        with self.assertRaisesRegex(
            ContractError, "unknown properties: impact|qualityExtensions|components"
        ):
            validate_project(document)

    def test_rejects_invalid_impact_references_and_portability(self):
        cases = (
            (
                {
                    "baseRefs": ["main"],
                    "unmatchedPaths": "all-scoped-checks",
                    "components": [
                        {
                            "id": "source",
                            "paths": ["src\\**"],
                            "affects": [],
                        }
                    ],
                },
                "portable relative glob",
            ),
            (
                {
                    "baseRefs": ["main"],
                    "unmatchedPaths": "all-scoped-checks",
                    "components": [
                        {
                            "id": "source",
                            "paths": ["src/**"],
                            "affects": ["missing"],
                        }
                    ],
                },
                "undefined components: missing",
            ),
        )
        for impact, message in cases:
            with self.subTest(message=message):
                document = self.valid_project()
                document["impact"] = impact
                with self.assertRaisesRegex(ContractError, message):
                    validate_project(document)

    def test_impact_contract_enforces_resource_bounds(self):
        document = self.valid_project()
        document["impact"] = {
            "baseRefs": [f"refs/heads/base-{index}" for index in range(17)],
            "unmatchedPaths": "all-scoped-checks",
            "components": [
                {"id": "source", "paths": ["src/**"], "affects": []}
            ],
        }
        with self.assertRaisesRegex(ContractError, "baseRefs: exceeds 16"):
            validate_project(document)

        document["impact"]["baseRefs"] = ["main"]
        document["impact"]["components"][0]["paths"] = [
            f"src/file-{index}.py" for index in range(65)
        ]
        with self.assertRaisesRegex(ContractError, "paths: exceeds 64"):
            validate_project(document)

        document["impact"]["components"][0]["paths"] = ["src/**suffix"]
        with self.assertRaisesRegex(ContractError, "portable relative glob"):
            validate_project(document)

        document = self.valid_project()
        document["impact"] = {
            "baseRefs": ["main"],
            "unmatchedPaths": "all-scoped-checks",
            "components": [
                {"id": "source", "paths": ["src/**"], "affects": []}
            ],
        }
        document["profiles"]["development"][0]["components"] = ["missing"]
        with self.assertRaisesRegex(ContractError, "undefined components: missing"):
            validate_project(document)

    def test_preserves_historical_project_manifest_versions(self):
        schema_one = self.valid_project()
        schema_one["schemaVersion"] = 1
        del schema_one["environment"]
        self.assertIsNone(validate_project(schema_one).environment)

        schema_two = self.valid_project()
        schema_two["schemaVersion"] = 2
        del schema_two["environment"]["foregroundOnly"]
        self.assertFalse(validate_project(schema_two).environment.foreground_only)

    def test_project_resource_bounds_use_a_new_schema_major(self):
        legacy = self.valid_project()
        legacy["schemaVersion"] = 1
        del legacy["environment"]
        check = legacy["profiles"]["development"][0]
        legacy["profiles"] = {
            f"profile-{index}": [dict(check)] for index in range(65)
        }
        legacy["lifecycle"]["requiredProfiles"] = ["profile-0"]
        self.assertEqual(65, len(validate_project(legacy).profiles))

        schema_three = self.valid_project()
        schema_three["schemaVersion"] = 3
        schema_three["profiles"] = {
            f"profile-{index}": [dict(check)] for index in range(65)
        }
        schema_three["lifecycle"]["requiredProfiles"] = ["profile-0"]
        schema_three["environment"]["profiles"] = {
            f"profile-{index}": ["python-runtime"] for index in range(65)
        }
        schema_three["environment"]["defaultProfile"] = "profile-0"
        self.assertEqual(65, len(validate_project(schema_three).profiles))

        bounded = self.valid_project()
        bounded["profiles"] = {
            f"profile-{index}": [dict(check)] for index in range(65)
        }
        bounded["lifecycle"]["requiredProfiles"] = ["profile-0"]
        bounded["environment"]["profiles"] = {
            f"profile-{index}": ["python-runtime"] for index in range(65)
        }
        with self.assertRaisesRegex(ContractError, "exceeds 64 profiles"):
            validate_project(bounded)

    def test_schema_two_batch_binding_remains_readable_for_manual_migration(self):
        document = self.valid_project()
        document["schemaVersion"] = 2
        del document["environment"]["foregroundOnly"]
        document["environment"]["managedTools"] = [
            {
                "id": "npm",
                "version": "1.0.0",
                "artifacts": [
                    {
                        "platform": "windows-x64",
                        "url": "https://downloads.example.test/npm.zip",
                        "checksum": f"sha256:{'0' * 64}",
                        "archiveFormat": "zip",
                        "stripComponents": 0,
                        "maxDownloadBytes": 1000,
                        "maxExtractedBytes": 2000,
                        "maxFiles": 20,
                        "commands": {"npm": "./bin/npm.cmd"},
                    }
                ],
            }
        ]

        project = validate_project(document)

        command = project.environment.managed_tools["npm"].artifacts[
            "windows-x64"
        ].commands["npm"]
        self.assertEqual("bin/npm.cmd", command.executable)
        self.assertIsNone(command.script)

    def test_rejects_shell_string(self):
        document = self.valid_project()
        document["profiles"]["development"][0]["run"] = "python -m unittest"

        with self.assertRaisesRegex(ContractError, "must contain at least"):
            validate_project(document)

    def test_rejects_escaping_working_directory(self):
        document = self.valid_project()
        document["profiles"]["development"][0]["workingDirectory"] = "../other"

        with self.assertRaisesRegex(ContractError, "must stay within"):
            validate_project(document)

    def test_rejects_unknown_properties(self):
        document = self.valid_project()
        document["agent"] = "codex"

        with self.assertRaisesRegex(ContractError, "unknown properties"):
            validate_project(document)

    def test_environment_requires_foreground_only_attestation(self):
        document = self.valid_project()
        document["environment"]["foregroundOnly"] = False

        with self.assertRaisesRegex(ContractError, "foregroundOnly: must attest true"):
            validate_project(document)

    def test_schema_three_windows_managed_commands_require_native_executables(self):
        document = self.valid_project()
        document["environment"]["managedTools"] = [
            {
                "id": "npm",
                "version": "1.0.0",
                "artifacts": [
                    {
                        "platform": "windows-x64",
                        "url": "https://downloads.example.test/npm.zip",
                        "checksum": f"sha256:{'0' * 64}",
                        "archiveFormat": "zip",
                        "stripComponents": 0,
                        "maxDownloadBytes": 1000,
                        "maxExtractedBytes": 2000,
                        "maxFiles": 20,
                        "commands": {"npm": "bin/npm.cmd"},
                    }
                ],
            }
        ]

        with self.assertRaisesRegex(ContractError, "native \\.exe"):
            validate_project(document)

        document["environment"]["managedTools"][0]["artifacts"][0]["commands"] = {
            "npm": {
                "executable": "node.exe",
                "script": "node_modules/npm/bin/npm-cli.js",
            }
        }
        project = validate_project(document)
        command = project.environment.managed_tools["npm"].artifacts[
            "windows-x64"
        ].commands["npm"]
        self.assertEqual("node.exe", command.executable)
        self.assertEqual("node_modules/npm/bin/npm-cli.js", command.script)

    def test_managed_command_paths_use_one_portable_relative_syntax(self):
        document = self.valid_project()
        document["environment"]["managedTools"] = [
            {
                "id": "npm",
                "version": "1.0.0",
                "artifacts": [
                    {
                        "platform": "windows-x64",
                        "url": "https://downloads.example.test/npm.zip",
                        "checksum": f"sha256:{'0' * 64}",
                        "archiveFormat": "zip",
                        "stripComponents": 0,
                        "maxDownloadBytes": 1000,
                        "maxExtractedBytes": 2000,
                        "maxFiles": 20,
                        "commands": {
                            "npm": {
                                "executable": "C:\\node.exe",
                                "script": "node_modules\\npm\\bin\\npm-cli.js",
                            }
                        },
                    }
                ],
            }
        ]

        with self.assertRaisesRegex(ContractError, "contained relative file path"):
            validate_project(document)


class ArtifactContractTests(unittest.TestCase):
    def valid_change(self):
        return {
            "schemaVersion": 3,
            "id": "production-change",
            "summary": "Exercise the production contract",
            "source": "test",
            "comparisonBase": "main",
            "specification": {
                "kind": "change-contract",
                "reference": "test",
                "rationale": "The fixture owns this bounded behavior.",
            },
            "risk": "medium",
            "affectedProjects": ["sample-project"],
            "acceptanceCriteria": [
                {"id": "ac-1", "outcome": "The production boundary is verified"}
            ],
            "requiredProfiles": ["review"],
            "quality": {
                "standard": "production-v1",
                "assessments": [
                    {
                        "dimension": dimension,
                        "status": "applicable",
                        "rationale": "The fixture verifies this dimension.",
                        "criteria": ["ac-1"],
                    }
                    for dimension in CORE_QUALITY_DIMENSIONS
                ],
            },
            "signOff": {
                "required": False,
                "status": "not-required",
                "evidence": None,
            },
        }

    def test_new_changes_require_every_production_dimension(self):
        document = self.valid_change()
        validate_change(document)

        document["quality"]["assessments"].pop()
        with self.assertRaisesRegex(ContractError, "missing core dimensions"):
            validate_change(document)

    def test_not_applicable_quality_requires_rationale_and_no_criteria(self):
        document = self.valid_change()
        privacy = next(
            item
            for item in document["quality"]["assessments"]
            if item["dimension"] == "privacy"
        )
        privacy["status"] = "not-applicable"
        with self.assertRaisesRegex(ContractError, "require an empty list"):
            validate_change(document)
        privacy["criteria"] = []
        validate_change(document)

    def test_release_identity_and_changes_derive_canonical_governed_release(self):
        document = {
            "schemaVersion": 2,
            "previousVersion": "0.1.1",
            "version": "0.2.0",
            "classification": "minor",
            "compatibility": "backward-compatible",
            "schemaImpact": "additive",
            "migration": None,
            "identity": {
                "package": "engineering-process",
                "distribution": "engineering_process",
                "tag": "v0.2.0",
                "releaseName": "v0.2.0",
                "runtimeVersion": {
                    "path": "engineering_process/__init__.py",
                    "variable": "VERSION",
                },
                "artifacts": [
                    "engineering_process-0.2.0-py3-none-any.whl",
                    "engineering_process-0.2.0.tar.gz",
                ],
                "receiptAsset": "engineering-process-v0.2.0-evidence.json",
            },
            "provenance": {
                "mode": "governed",
                "statement": "The receipt binds the release.",
                "lifecycleReceipt": {
                    "asset": "engineering-process-v0.2.0-evidence.json",
                    "project": "engineering-process",
                    "changeId": "release-0-2-0",
                    "cycle": 2,
                },
            },
            "changes": [
                {
                    "id": "portable-evidence",
                    "type": "capability",
                    "surfaces": ["evidence"],
                    "rationale": "Add portable evidence.",
                }
            ],
        }
        release = validate_release(document)
        self.assertEqual("v0.2.0", release.release_name)

        document["identity"]["releaseName"] = "engineering-process 0.2.0"
        with self.assertRaisesRegex(ContractError, "governed releases must use v0.2.0"):
            validate_release(document)

        document["identity"]["releaseName"] = "v0.2.0"
        document["classification"] = "patch"
        document["version"] = "0.1.2"
        document["identity"]["tag"] = "v0.1.2"
        document["identity"]["releaseName"] = "v0.1.2"
        document["identity"]["artifacts"] = [
            "engineering_process-0.1.2-py3-none-any.whl",
            "engineering_process-0.1.2.tar.gz",
        ]
        document["identity"]["receiptAsset"] = "engineering-process-v0.1.2-evidence.json"
        document["provenance"]["lifecycleReceipt"]["asset"] = document["identity"]["receiptAsset"]
        with self.assertRaisesRegex(ContractError, "changes require minor"):
            validate_release(document)
    def test_release_requires_the_exact_declared_semver_increment(self):
        document = {
            "schemaVersion": 1,
            "previousVersion": "0.1.1",
            "version": "0.2.0",
            "classification": "minor",
            "compatibility": "backward-compatible",
            "schemaImpact": "additive",
            "migration": None,
        }

        release = validate_release(document)

        self.assertEqual("0.2.0", release.version)
        document["version"] = "0.4.0"
        with self.assertRaisesRegex(ContractError, "must be 0.2.0"):
            validate_release(document)

    def test_release_requires_compatible_patch_and_migration_for_breaking_change(self):
        document = {
            "schemaVersion": 1,
            "previousVersion": "1.2.3",
            "version": "1.2.4",
            "classification": "patch",
            "compatibility": "incompatible",
            "schemaImpact": "breaking",
            "migration": "Migrate the project contract before upgrade.",
        }
        with self.assertRaisesRegex(ContractError, "patch release"):
            validate_release(document)

        document.update(
            version="2.0.0",
            classification="major",
            migration=None,
        )
        with self.assertRaisesRegex(ContractError, "require guidance"):
            validate_release(document)

    def test_stable_incompatible_minor_is_rejected(self):
        with self.assertRaisesRegex(ContractError, "requires a major"):
            validate_release(
                {
                    "schemaVersion": 1,
                    "previousVersion": "1.2.3",
                    "version": "1.3.0",
                    "classification": "minor",
                    "compatibility": "incompatible",
                    "schemaImpact": "unchanged",
                    "migration": "Replace the removed command.",
                }
            )

    def test_lock_requires_sorted_skills_and_digest(self):
        with self.assertRaisesRegex(ContractError, "must be sorted"):
            validate_process_lock(
                {
                    "schemaVersion": 1,
                    "process": {
                        "version": "0.1.0",
                        "digest": f"sha256:{'0' * 64}",
                    },
                    "skills": ["verify-change", "define-change-contract"],
                }
            )

    def test_change_requires_approval_evidence(self):
        document = {
            "schemaVersion": 2,
            "id": "sec-12",
            "summary": "Change authentication policy",
            "source": "SEC-12",
            "comparisonBase": "main",
            "specification": {
                "kind": "project",
                "reference": "docs/security.md",
                "rationale": "The security contract owns the behavior",
            },
            "risk": "high",
            "affectedProjects": ["sample-project"],
            "acceptanceCriteria": [
                {"id": "ac-1", "outcome": "Unauthorized access is rejected"}
            ],
            "requiredProfiles": ["review"],
            "signOff": {
                "required": True,
                "status": "approved",
                "evidence": None,
            },
        }

        with self.assertRaisesRegex(ContractError, "non-empty trimmed string"):
            validate_change(document)

    def test_approved_review_cannot_have_open_findings(self):
        document = {
            "schemaVersion": 2,
            "changeId": "sec-12",
            "cycle": 1,
            "checkpoint": "abc",
            "workspaceFingerprint": f"sha256:{'0' * 64}",
            "comparisonBase": "main",
            "reviewer": {
                "actorId": "reviewer",
                "contextId": "review-context",
                "kind": "agent",
            },
            "independence": {
                "method": "isolated-context",
                "attestedBy": "test-host",
                "evidence": "A separate context was created",
            },
            "verdict": "approved",
            "findings": [
                {
                    "id": "finding-1",
                    "severity": "high",
                    "path": "src/auth.py",
                    "line": 10,
                    "summary": "Authorization is missing",
                    "evidence": "The handler has no policy check",
                    "status": "open",
                    "resolutionEvidence": None,
                }
            ],
        }

        with self.assertRaisesRegex(ContractError, "open or deferred"):
            validate_review(document)

        document["findings"][0]["status"] = "deferred"
        document["findings"][0]["resolutionEvidence"] = (
            "A later change was proposed without owner approval"
        )
        with self.assertRaisesRegex(ContractError, "open or deferred"):
            validate_review(document)

    def test_plan_rejects_unknown_work_item_mapping(self):
        document = {
            "schemaVersion": 1,
            "changeId": "change-1",
            "contractDigest": f"sha256:{'0' * 64}",
            "approach": "Use the current owner",
            "workItems": [
                {
                    "id": "work-1",
                    "outcome": "Implement it",
                    "affectedPaths": ["src/"],
                    "verificationProfiles": ["development"],
                }
            ],
            "acceptancePlan": [
                {
                    "criterionId": "ac-1",
                    "workItems": ["missing"],
                    "verificationProfiles": ["development"],
                }
            ],
            "risks": [],
            "openDecisions": [],
        }

        with self.assertRaisesRegex(ContractError, "unknown ids"):
            validate_plan(document)

    def test_plan_resource_bounds_use_a_new_schema_major(self):
        work_items = [
            {
                "id": f"work-{index}",
                "outcome": "Implement it",
                "affectedPaths": ["src/"],
                "verificationProfiles": ["development"],
            }
            for index in range(257)
        ]
        document = {
            "schemaVersion": 1,
            "changeId": "change-1",
            "contractDigest": f"sha256:{'0' * 64}",
            "approach": "Use the current owner",
            "workItems": work_items,
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

        validate_plan(document)
        document["schemaVersion"] = 2
        with self.assertRaisesRegex(ContractError, "exceeds 256"):
            validate_plan(document)
