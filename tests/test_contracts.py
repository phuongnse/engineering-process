import copy
import hashlib
import json
import unittest
from pathlib import Path

from engineering_process.contracts import (
    CORE_QUALITY_DIMENSIONS,
    ContractError,
    canonical_json_digest,
    derive_release_version,
    validate_adoption_migration,
    validate_automation_policy,
    validate_automation_proposal,
    validate_automation_proposal_policy,
    validate_change,
    validate_improvement_catalog,
    validate_improvement_disposition,
    validate_improvement_reproduction,
    validate_improvement_resolution,
    validate_improvement_signal,
    validate_plan,
    validate_plan_decision_review,
    validate_plan_decision_review_assignment,
    validate_process_lock,
    validate_project,
    validate_release,
    validate_release_change,
    validate_remote_verification_evidence,
    validate_remote_verification_request,
    validate_review,
)
from engineering_process.transition import (
    validate_authority_transition_evidence,
    validate_authority_transition_request,
    validate_bootstrap_adoption_consumption,
    validate_bootstrap_adoption_intent,
    validate_protected_transition_policy,
)


PROCESS_ROOT = Path(__file__).resolve().parent.parent


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

    def test_plan_decision_policy_is_additive_and_canonical(self):
        document = self.valid_project()
        categories = [
            "architecture",
            "authority",
            "compatibility",
            "external-mutation",
            "lifecycle-order",
            "owner",
            "rollout",
            "scope",
            "trust-boundary",
        ]
        document["lifecycle"]["planDecision"] = {
            "mode": "provenance-gated-authored-review",
            "materialCategories": categories,
        }

        project = validate_project(document)

        self.assertEqual(
            "provenance-gated-authored-review", project.plan_decision_mode
        )
        self.assertEqual(tuple(categories), project.material_decision_categories)
        document["schemaVersion"] = 2
        document["environment"].pop("foregroundOnly")
        with self.assertRaisesRegex(ContractError, "unknown properties"):
            validate_project(document)

    def test_plan_schema_three_requires_explicit_provenance(self):
        document = json.loads(
            (PROCESS_ROOT / "examples" / "plan.json").read_text(encoding="utf-8")
        )
        validate_plan(document)
        del document["provenance"]
        with self.assertRaisesRegex(ContractError, "provenance"):
            validate_plan(document)

        document["schemaVersion"] = 2
        validate_plan(document)

    def test_plan_decision_artifacts_derive_verdict_from_all_categories(self):
        assignment = json.loads(
            (
                PROCESS_ROOT
                / "examples"
                / "plan-decision-review-assignment.json"
            ).read_text(encoding="utf-8")
        )
        review = json.loads(
            (PROCESS_ROOT / "examples" / "plan-decision-review.json").read_text(
                encoding="utf-8"
            )
        )
        validate_plan_decision_review_assignment(assignment)
        validate_plan_decision_review(review)
        review["categoryAssessments"][0]["status"] = "decision-required"
        with self.assertRaisesRegex(ContractError, "must be derived"):
            validate_plan_decision_review(review)

    def test_improvement_contracts_validate_packaged_examples(self):
        validators = {
            "improvement-catalog": validate_improvement_catalog,
            "improvement-disposition": validate_improvement_disposition,
            "improvement-reproduction": validate_improvement_reproduction,
            "improvement-resolution": validate_improvement_resolution,
            "improvement-signal": validate_improvement_signal,
        }
        for name, validator in validators.items():
            with self.subTest(name=name):
                document = json.loads(
                    (PROCESS_ROOT / "examples" / f"{name}.json").read_text(
                        encoding="utf-8"
                    )
                )
                validator(document)

    def test_improvement_signal_rejects_authority_and_raw_evidence(self):
        document = json.loads(
            (PROCESS_ROOT / "examples" / "improvement-signal.json").read_text(
                encoding="utf-8"
            )
        )
        document["controls"]["grantsAuthority"] = True
        with self.assertRaisesRegex(ContractError, "grant no authority"):
            validate_improvement_signal(document)

        document["controls"]["grantsAuthority"] = False
        document["controls"]["rawOutputIncluded"] = True
        with self.assertRaisesRegex(ContractError, "raw sensitive evidence"):
            validate_improvement_signal(document)

    def test_recurring_non_shared_disposition_requires_owner_exception(self):
        document = json.loads(
            (
                PROCESS_ROOT / "examples" / "improvement-disposition.json"
            ).read_text(encoding="utf-8")
        )
        document["ownerBoundary"] = "project-local"
        document["requiredProof"] = {
            "producerLifecycle": False,
            "immutableRelease": False,
            "consumerReproduction": False,
        }
        with self.assertRaisesRegex(ContractError, "requires owner approval"):
            validate_improvement_disposition(document)

    def test_automation_proposal_requires_exact_safe_controls_and_policy_digest(self):
        document = json.loads(
            (PROCESS_ROOT / "examples" / "automation-proposal.json").read_text(
                encoding="utf-8"
            )
        )

        proposal = validate_automation_proposal(document)

        self.assertEqual("renovate", proposal.automation_owner)
        self.assertEqual("lifecycle-completion", proposal.completion_check)

        document["observedControls"]["scripts"] = True
        with self.assertRaisesRegex(ContractError, "scripts: must be false"):
            validate_automation_proposal(document)

        document["observedControls"]["scripts"] = False
        document["optIn"]["document"]["allowedAutomationOwners"] = ["other"]
        with self.assertRaisesRegex(ContractError, "canonical policy document"):
            validate_automation_proposal(document)

    def test_standing_automation_policy_requires_complete_gated_authority(self):
        policy = json.loads(
            (PROCESS_ROOT / "examples" / "automation-policy.json").read_text(
                encoding="utf-8"
            )
        )

        validated = validate_automation_policy(policy)

        self.assertEqual("exceptions-only", validated["confirmationMode"])
        self.assertEqual("squash", validated["merge"]["method"])
        self.assertIn("merge", validated["actions"])

        policy["actions"].remove("merge")
        with self.assertRaisesRegex(ContractError, "complete sorted standing action"):
            validate_automation_policy(policy)

        policy = json.loads(
            (PROCESS_ROOT / "examples" / "automation-policy.json").read_text(
                encoding="utf-8"
            )
        )
        policy["merge"]["requireIndependentReview"] = False
        with self.assertRaisesRegex(ContractError, "requireIndependentReview: must be true"):
            validate_automation_policy(policy)

        policy = json.loads(
            (PROCESS_ROOT / "examples" / "automation-policy.json").read_text(
                encoding="utf-8"
            )
        )
        policy["escalationReasons"].append("routine-confirmation")
        with self.assertRaises(ContractError):
            validate_automation_policy(policy)

        policy = json.loads(
            (PROCESS_ROOT / "examples" / "automation-policy.json").read_text(
                encoding="utf-8"
            )
        )
        policy["schemaVersion"] = True
        with self.assertRaisesRegex(ContractError, "schemaVersion: must be 1"):
            validate_automation_policy(policy)

    def test_automation_proposal_policy_is_an_explicit_strict_opt_in(self):
        policy = json.loads(
            (
                PROCESS_ROOT / "examples" / "automation-proposal-policy.json"
            ).read_text(encoding="utf-8")
        )

        validate_automation_proposal_policy(policy)
        self.assertFalse(policy["requiredControls"]["humanMergeRequired"])

        policy["enabled"] = False
        with self.assertRaisesRegex(ContractError, "enabled: must be true"):
            validate_automation_proposal_policy(policy)

        policy["enabled"] = True
        policy["requiredControls"]["writeCapableChecks"] = True
        with self.assertRaisesRegex(ContractError, "writeCapableChecks: must be false"):
            validate_automation_proposal_policy(policy)

    def test_historical_human_only_proposal_policy_remains_readable(self):
        policy = json.loads(
            (
                PROCESS_ROOT / "examples" / "automation-proposal-policy.json"
            ).read_text(encoding="utf-8")
        )
        policy["schemaVersion"] = 1
        policy["requiredControls"]["humanMergeRequired"] = True

        validate_automation_proposal_policy(policy)

        proposal = json.loads(
            (PROCESS_ROOT / "examples" / "automation-proposal.json").read_text(
                encoding="utf-8"
            )
        )
        proposal["schemaVersion"] = 1
        proposal["observedControls"]["humanMergeRequired"] = True
        proposal["optIn"]["document"] = copy.deepcopy(policy)
        proposal["optIn"]["sha256"] = canonical_json_digest(policy)

        validated = validate_automation_proposal(proposal)

        self.assertEqual(1, validated.schema_version)
        self.assertTrue(validated.human_merge_required)

        proposal["schemaVersion"] = True
        proposal["optIn"]["document"]["schemaVersion"] = True
        with self.assertRaisesRegex(ContractError, "schemaVersion: must be 1, 2, or 3"):
            validate_automation_proposal(proposal)

    def test_process_adoption_proposal_requires_consumer_owner_merge(self):
        document = json.loads(
            (
                PROCESS_ROOT
                / "examples"
                / "automation-process-adoption-proposal.json"
            ).read_text(encoding="utf-8")
        )

        proposal = validate_automation_proposal(document)

        self.assertEqual(3, proposal.schema_version)
        self.assertEqual("process-adoption", proposal.proposal_kind)
        self.assertIsNone(proposal.human_merge_required)
        self.assertTrue(proposal.consumer_owner_merge_required)
        self.assertEqual("consumer-owner-merge", proposal.completion_check)

        document["observedControls"]["automerge"] = True
        with self.assertRaisesRegex(ContractError, "automerge: must be false"):
            validate_automation_proposal(document)

    def test_process_adoption_proposal_rejects_merge_escalation_and_partial_evidence(self):
        source = json.loads(
            (
                PROCESS_ROOT
                / "examples"
                / "automation-process-adoption-proposal.json"
            ).read_text(encoding="utf-8")
        )

        cases = []
        completion = copy.deepcopy(source)
        completion["optIn"]["document"]["completionCheck"] = "lifecycle-completion"
        completion["optIn"]["sha256"] = canonical_json_digest(
            completion["optIn"]["document"]
        )
        cases.append((completion, "consumer-owner-merge"))

        post_merge = copy.deepcopy(source)
        post_merge["processAdoption"]["materialization"]["postMergeActions"] = [
            "synchronize"
        ]
        cases.append((post_merge, "merge is terminal"))

        omitted = copy.deepcopy(source)
        omitted["processAdoption"]["managedFiles"] = omitted[
            "processAdoption"
        ]["managedFiles"][1:]
        omitted["processAdoption"]["managedDistributionSha256"] = canonical_json_digest(
            omitted["processAdoption"]["managedFiles"]
        )
        cases.append((omitted, "complete fixed"))

        verifier = copy.deepcopy(source)
        verifier["verifier"]["commit"] = "9" * 40
        cases.append((verifier, "protected-base opt-in verifier"))

        for document, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ContractError, message):
                    validate_automation_proposal(document)

    def test_process_adoption_policy_does_not_use_human_actor_language(self):
        policy = json.loads(
            (
                PROCESS_ROOT
                / "examples"
                / "automation-process-adoption-policy.json"
            ).read_text(encoding="utf-8")
        )

        validated = validate_automation_proposal_policy(policy)

        self.assertNotIn("humanMergeRequired", validated["requiredControls"])
        self.assertTrue(
            validated["requiredControls"]["consumerOwnerMergeRequired"]
        )
        self.assertFalse(validated["requiredControls"]["postMergeMutation"])

    def test_process_adoption_rejects_invented_producer_release_evidence(self):
        document = json.loads(
            (
                PROCESS_ROOT
                / "examples"
                / "automation-process-adoption-proposal.json"
            ).read_text(encoding="utf-8")
        )
        binding = document["processAdoption"]["producerRelease"][
            "distributionAttestation"
        ]
        attestation = json.loads(binding["content"])
        attestation["checkpoint"] = "4" * 40
        binding["content"] = (
            json.dumps(attestation, separators=(",", ":"), sort_keys=True) + "\n"
        )
        binding["sha256"] = "sha256:" + hashlib.sha256(
            binding["content"].encode("utf-8")
        ).hexdigest()

        with self.assertRaisesRegex(ContractError, "attestation identity"):
            validate_automation_proposal(document)

        document = json.loads(
            (
                PROCESS_ROOT
                / "examples"
                / "automation-process-adoption-proposal.json"
            ).read_text(encoding="utf-8")
        )
        binding = document["processAdoption"]["producerRelease"][
            "distributionAttestation"
        ]
        attestation = json.loads(binding["content"])
        attestation["lifecycleReceipt"]["changeId"] = "different-release"
        attestation["lifecycleReceipt"]["cycle"] = 9
        binding["content"] = (
            json.dumps(attestation, separators=(",", ":"), sort_keys=True) + "\n"
        )
        binding["sha256"] = "sha256:" + hashlib.sha256(
            binding["content"].encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(ContractError, "lifecycle provenance"):
            validate_automation_proposal(document)

        document = json.loads(
            (
                PROCESS_ROOT
                / "examples"
                / "automation-process-adoption-proposal.json"
            ).read_text(encoding="utf-8")
        )
        document["processAdoption"]["producerRelease"]["materialization"][
            "processDigest"
        ] = f"sha256:{'0' * 64}"
        with self.assertRaisesRegex(ContractError, "exact target"):
            validate_automation_proposal(document)

        document = json.loads(
            (
                PROCESS_ROOT
                / "examples"
                / "automation-process-adoption-proposal.json"
            ).read_text(encoding="utf-8")
        )
        document["processAdoption"]["producerRelease"]["repository"] = (
            "attacker/process"
        )
        document["processAdoption"]["actionPins"][0]["repository"] = (
            "attacker/process"
        )
        with self.assertRaisesRegex(ContractError, "protected-base producer"):
            validate_automation_proposal(document)

    def test_automation_proposal_bounds_and_canonicalizes_changed_paths(self):
        document = json.loads(
            (PROCESS_ROOT / "examples" / "automation-proposal.json").read_text(
                encoding="utf-8"
            )
        )
        document["changedPaths"] = ["package.json", "package-lock.json"]
        with self.assertRaisesRegex(ContractError, "sorted and unique"):
            validate_automation_proposal(document)

        document["changedPaths"] = [f"locks/dependency-{index}.lock" for index in range(1001)]
        with self.assertRaisesRegex(ContractError, "between 1 and 1000"):
            validate_automation_proposal(document)

    def test_adoption_migration_binds_distinct_final_versions_and_project(self):
        project = self.valid_project()
        target_content = (
            json.dumps(project, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        document = {
            "schemaVersion": 1,
            "fromProcessVersion": "0.1.1",
            "toProcessVersion": "0.2.0",
            "sourceProjectDigest": f"sha256:{'0' * 64}",
            "targetProjectDigest": (
                "sha256:" + hashlib.sha256(target_content).hexdigest()
            ),
            "project": project,
        }

        validate_adoption_migration(document)

        document["toProcessVersion"] = "0.1.1"
        with self.assertRaisesRegex(ContractError, "must differ"):
            validate_adoption_migration(document)

        document["toProcessVersion"] = "0.2.0"
        document["project"]["schemaVersion"] = 99
        with self.assertRaisesRegex(ContractError, "schemaVersion"):
            validate_adoption_migration(document)

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
    def test_remote_transition_uses_new_request_and_evidence_majors(self):
        transition = {
            "request": {"path": ".process/runs/change/request.json", "digest": f"sha256:{'1' * 64}"},
            "candidateEvidence": {"path": ".process/runs/change/evidence.json", "digest": f"sha256:{'2' * 64}"},
        }
        request = json.loads((PROCESS_ROOT / "examples" / "remote-verification-request.json").read_text(encoding="utf-8"))
        request["schemaVersion"] = 2
        request["authorityTransition"] = transition
        validate_remote_verification_request(request)
        evidence = json.loads((PROCESS_ROOT / "examples" / "remote-verification-evidence.json").read_text(encoding="utf-8"))
        evidence["schemaVersion"] = 2
        evidence["authorityTransition"] = transition
        validate_remote_verification_evidence(evidence)

    def test_authority_transition_contracts_validate_packaged_examples(self):
        validators = {
            "authority-transition-evidence": validate_authority_transition_evidence,
            "authority-transition-request": validate_authority_transition_request,
            "bootstrap-adoption-consumption": validate_bootstrap_adoption_consumption,
            "bootstrap-adoption-intent": validate_bootstrap_adoption_intent,
            "protected-transition-policy": validate_protected_transition_policy,
        }
        for name, validator in validators.items():
            with self.subTest(name=name):
                document = json.loads(
                    (PROCESS_ROOT / "examples" / f"{name}.json").read_text(
                        encoding="utf-8"
                    )
                )
                validator(document)

    def test_transition_request_rejects_same_authority_and_partial_paths(self):
        document = json.loads(
            (PROCESS_ROOT / "examples" / "authority-transition-request.json").read_text(
                encoding="utf-8"
            )
        )
        document["target"]["version"] = "0.7.0"
        document["target"]["tag"] = "v0.7.0"
        with self.assertRaisesRegex(ContractError, "must differ"):
            validate_authority_transition_request(document)
        document["target"]["version"] = "0.9.0"
        document["target"]["tag"] = "v0.9.0"
        document["candidate"]["expectedChangedPaths"] = []
        with self.assertRaisesRegex(ContractError, "non-empty"):
            validate_authority_transition_request(document)

    def test_protected_transition_policy_is_exactly_single_use(self):
        document = json.loads(
            (PROCESS_ROOT / "examples" / "protected-transition-policy.json").read_text(
                encoding="utf-8"
            )
        )
        document["singleUse"] = False
        with self.assertRaisesRegex(ContractError, "single-use"):
            validate_protected_transition_policy(document)
        document["singleUse"] = True
        document["sourceBase"] = "0" * 40
        with self.assertRaisesRegex(ContractError, "unknown properties"):
            validate_protected_transition_policy(document)

    def test_release_schema_four_binds_stale_source_authority(self):
        document = {
            "schemaVersion": 4,
            "previousVersion": "0.8.0",
            "version": "0.9.0",
            "classification": "minor",
            "compatibility": "incompatible",
            "schemaImpact": "breaking",
            "migration": "Use the authority-transition protocol for trust-root cutovers.",
            "identity": {
                "package": "engineering-process",
                "distribution": "engineering_process",
                "tag": "v0.9.0",
                "releaseName": "v0.9.0",
                "runtimeVersion": {"path": "engineering_process/__init__.py", "variable": "VERSION"},
                "artifacts": ["engineering_process-0.9.0-py3-none-any.whl", "engineering_process-0.9.0.tar.gz"],
                "receiptAsset": "engineering-process-v0.9.0-evidence.json",
                "authorizationAsset": None,
            },
            "provenance": {
                "mode": "authority-transition-bootstrap",
                "statement": "Public 0.7.0 governs the transition release.",
                "lifecycleReceipt": {"asset": "engineering-process-v0.9.0-evidence.json", "project": "engineering-process", "changeId": "release-0-9-0", "cycle": 1},
                "authorityTransition": {
                    "sourceAuthority": {"version": "0.7.0", "digest": f"sha256:{'0' * 64}"},
                    "skippedRelease": {"version": "0.8.0", "tag": "v0.8.0"},
                    "bootstrapChangeId": "authority-transition-protocol",
                },
            },
            "changes": [{"id": "authority-transition-protocol", "type": "breaking", "surfaces": ["lifecycle"], "rationale": "Introduce exact transitions."}],
        }
        release = validate_release(document)
        self.assertEqual("0.7.0", release.transition_source_version)
        document["provenance"]["authorityTransition"]["sourceAuthority"]["version"] = "0.8.0"
        with self.assertRaisesRegex(ContractError, "stale source authority"):
            validate_release(document)

    def test_release_schema_three_separates_bootstrap_authorization_from_receipts(self):
        document = {
            "schemaVersion": 3,
            "previousVersion": "0.1.1",
            "version": "0.2.0",
            "classification": "minor",
            "compatibility": "backward-compatible",
            "schemaImpact": "additive",
            "migration": None,
            "identity": {
                "package": "sample",
                "distribution": "sample",
                "tag": "v0.2.0",
                "releaseName": "v0.2.0",
                "runtimeVersion": {"path": "sample.py", "variable": "VERSION"},
                "artifacts": [
                    "sample-0.2.0-py3-none-any.whl",
                    "sample-0.2.0.tar.gz",
                ],
                "receiptAsset": None,
                "authorizationAsset": "sample-v0.2.0-bootstrap-authorization.json",
            },
            "provenance": {
                "mode": "bootstrap-authority",
                "statement": "One reviewed bootstrap authority.",
                "lifecycleReceipt": None,
            },
            "changes": [
                {
                    "id": "publish-authority",
                    "type": "capability",
                    "surfaces": ["publication"],
                    "rationale": "Publish the lifecycle evidence authority.",
                }
            ],
        }

        release = validate_release(document)

        self.assertEqual("bootstrap-authority", release.provenance_mode)
        document["identity"]["receiptAsset"] = "sample-v0.2.0-evidence.json"
        with self.assertRaisesRegex(ContractError, "must use null"):
            validate_release(document)

    def test_release_change_requires_migration_for_breaking_behavior(self):
        document = {
            "schemaVersion": 1,
            "id": "remove-command",
            "type": "breaking",
            "surfaces": ["cli"],
            "rationale": "Remove the retired command.",
            "schemaImpact": "unchanged",
            "migration": "Use processctl replacement instead.",
        }

        change = validate_release_change(document)

        self.assertEqual("remove-command", change.identifier)
        document["migration"] = None
        with self.assertRaisesRegex(ContractError, "require guidance"):
            validate_release_change(document)

    def test_release_version_is_derived_from_highest_public_change(self):
        cases = (
            ("0.1.1", ["fix"], "0.1.2", "patch", "backward-compatible"),
            (
                "0.1.1",
                ["fix", "capability"],
                "0.2.0",
                "minor",
                "backward-compatible",
            ),
            ("0.1.1", ["breaking"], "0.2.0", "minor", "incompatible"),
            ("1.4.2", ["breaking"], "2.0.0", "major", "incompatible"),
        )
        for previous, changes, version, classification, compatibility in cases:
            with self.subTest(previous=previous, changes=changes):
                plan = derive_release_version(previous, changes)
                self.assertEqual(version, plan.version)
                self.assertEqual(classification, plan.classification)
                self.assertEqual(compatibility, plan.compatibility)

    def test_release_version_planning_rejects_missing_or_invalid_inputs(self):
        with self.assertRaisesRegex(ContractError, "at least one change type"):
            derive_release_version("0.1.1", [])
        with self.assertRaisesRegex(ContractError, "unknown release change types"):
            derive_release_version("0.1.1", ["progress"])
        with self.assertRaisesRegex(ContractError, "final SemVer"):
            derive_release_version("0.2.0-rc.1", ["fix"])

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
