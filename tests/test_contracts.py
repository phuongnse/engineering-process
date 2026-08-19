import unittest

from engineering_process.contracts import (
    ContractError,
    validate_change,
    validate_plan,
    validate_process_lock,
    validate_project,
    validate_review,
)


class ProjectContractTests(unittest.TestCase):
    def valid_project(self):
        return {
            "schemaVersion": 1,
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

    def test_rejects_a_second_project_manifest_version_during_development(self):
        document = self.valid_project()
        document["schemaVersion"] = 2

        with self.assertRaisesRegex(ContractError, "schemaVersion: must be 1"):
            validate_project(document)

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


class ArtifactContractTests(unittest.TestCase):
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
            "schemaVersion": 1,
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
            "schemaVersion": 1,
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
