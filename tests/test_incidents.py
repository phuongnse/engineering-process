"""Unit and contract tests for automated process-improvement incident intake."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from engineering_process import VERSION
from engineering_process.incidents import (
    CLOSED_TAXONOMY,
    Incident,
    collect_incidents,
    is_process_producer_change,
    process_improvement_intake,
    render_sanitized_issue_body,
    resolve_consumer_identity,
    resolve_process_repo,
    stable_title_key,
)


class IncidentIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temporary.name)
        process_dir = self.project_root / ".process"
        process_dir.mkdir(parents=True)
        project = {
            "schemaVersion": 1,
            "project": "consumer",
            "lifecycle": {
                "requiredProfiles": ["development"],
                "processChanges": {
                    "requireConsumerEvidence": True,
                    "acceptedIssueUrlPrefix": "https://github.com/phuongnse/engineering-process/issues/",
                },
            },
            "profiles": {
                "development": [
                    {"id": "unit", "run": ["python", "-c", "pass"], "timeoutSeconds": 10}
                ]
            },
        }
        (process_dir / "project.json").write_text(
            json.dumps(project), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_closed_taxonomy_contains_canonical_kinds(self) -> None:
        expected = {
            "evidence-integrity",
            "execution-boundary",
            "governance-thrashing",
            "invariant-violation",
            "publication-boundary",
            "explicit-review-signal",
        }
        self.assertEqual(expected, set(CLOSED_TAXONOMY))

    def test_stable_title_key_formatting_and_sanitization(self) -> None:
        key = stable_title_key("org/my-consumer", "2.6.0", "evidence-drop", "evidence-integrity")
        self.assertEqual(
            "[consumer-process][org-my-consumer][2.6.0][evidence-drop][evidence-integrity]",
            key,
        )

    def test_collect_incidents_detects_evidence_invalidation(self) -> None:
        state = {
            "history": [
                {
                    "event": "evidence-invalidated",
                    "details": {"profile": "development", "reason": "input-digest-mismatch"},
                }
            ],
            "verification": {},
            "cycle": 1,
        }
        incidents = collect_incidents(Path.cwd(), Path.cwd(), state)
        self.assertEqual(1, len(incidents))
        self.assertEqual("evidence-integrity", incidents[0].kind)
        self.assertEqual("development", incidents[0].invariant)

    def test_collect_incidents_detects_redundant_verification_in_same_cycle(self) -> None:
        state = {
            "history": [
                {"event": "profile-verified", "details": {"profile": "development"}},
                {"event": "profile-verified", "details": {"profile": "review"}},
                {"event": "profile-verified", "details": {"profile": "development"}},
            ],
            "verification": {},
            "cycle": 1,
        }
        incidents = collect_incidents(Path.cwd(), Path.cwd(), state)
        kinds = [i.kind for i in incidents]
        self.assertIn("evidence-integrity", kinds)
        repetition = next(i for i in incidents if i.invariant == "verification-repetition")
        self.assertEqual(2, repetition.details["verificationCount"])

    def test_collect_incidents_detects_execution_boundary_failures(self) -> None:
        state = {
            "history": [],
            "verification": {
                "development": {
                    "checks": [
                        {
                            "id": "long-test",
                            "timedOut": True,
                            "outputExceeded": False,
                            "descendantsTerminated": False,
                            "streamFailed": False,
                        },
                        {
                            "id": "noisy-test",
                            "timedOut": False,
                            "outputExceeded": True,
                            "descendantsTerminated": False,
                            "streamFailed": False,
                        },
                        {
                            "id": "leaky-test",
                            "timedOut": False,
                            "outputExceeded": False,
                            "descendantsTerminated": True,
                            "streamFailed": False,
                        },
                    ]
                }
            },
            "cycle": 1,
        }
        incidents = collect_incidents(Path.cwd(), Path.cwd(), state)
        invariants = {i.invariant for i in incidents}
        self.assertIn("check-timeout", invariants)
        self.assertIn("output-overflow", invariants)
        self.assertIn("descendants-terminated", invariants)

    def test_collect_incidents_detects_governance_thrashing(self) -> None:
        state = {
            "cycle": 3,
            "history": [
                {
                    "event": "review-assignment-replaced",
                    "details": {"previousReviewer": "agent-1", "newReviewer": "agent-2"},
                }
            ],
            "verification": {},
        }
        incidents = collect_incidents(Path.cwd(), Path.cwd(), state)
        invariants = {i.invariant for i in incidents}
        self.assertIn("excessive-review-cycles", invariants)
        self.assertIn("reviewer-context-replaced", invariants)

    def test_collect_incidents_detects_invariant_violation_and_signals(self) -> None:
        state = {
            "cycle": 1,
            "history": [],
            "verification": {},
            "review": {
                "document": {
                    "productionEngineering": [
                        {
                            "id": "single-policy-authority",
                            "status": "violated",
                            "rationale": "Duplicated policy across adapters",
                        }
                    ],
                    "processImprovement": {
                        "status": "shared-process",
                        "rationale": "Missing shared adoption guidance",
                    },
                }
            },
        }
        incidents = collect_incidents(Path.cwd(), Path.cwd(), state)
        invariants = {i.invariant for i in incidents}
        self.assertIn("single-policy-authority", invariants)
        self.assertIn("shared-process-finding", invariants)

    def test_render_sanitized_issue_body_strips_sensitive_data(self) -> None:
        incident = Incident(
            kind="evidence-integrity",
            invariant="development",
            summary="Verification evidence was invalidated",
            details={
                "profile": "development",
                "secret_token": "ghp_secret12345",
                "api_key": "private_key_abc",
                "digest": "sha256:1234",
            },
        )
        body = render_sanitized_issue_body("test-org/test-repo", "2.6.0", incident, {})
        self.assertNotIn("ghp_secret12345", body)
        self.assertNotIn("private_key_abc", body)
        self.assertIn("development", body)
        self.assertIn("sha256:1234", body)

    def test_process_improvement_intake_reuses_existing_tracker_issue(self) -> None:
        state = {
            "changeId": "test-change",
            "cycle": 1,
            "contract": {"document": {"affectedProjects": ["some-consumer"], "source": "https://example.com"}},
            "history": [
                {
                    "event": "evidence-invalidated",
                    "details": {"profile": "development", "reason": "input-digest-mismatch"},
                }
            ],
            "verification": {},
        }
        actor = {"actorId": "coordinator", "contextId": "finish", "kind": "agent"}

        mock_matches = [
            {
                "number": 214,
                "title": "[consumer-process][test][2.6.0][development][evidence-integrity] Verification issue",
                "url": "https://github.com/phuongnse/engineering-process/issues/214",
                "state": "OPEN",
            }
        ]

        results = process_improvement_intake(
            self.project_root,
            Path.cwd(),
            state,
            actor,
            search_tracker_fn=lambda repo, q: mock_matches,
            create_issue_fn=lambda repo, t, b: "https://github.com/phuongnse/engineering-process/issues/999",
        )

        self.assertEqual(1, len(results))
        self.assertEqual("reused", results[0]["status"])
        self.assertEqual("https://github.com/phuongnse/engineering-process/issues/214", results[0]["recordUrl"])

        events = [e["event"] for e in state["history"]]
        self.assertIn("incident-collected", events)
        self.assertIn("process-improvement-reused", events)

    def test_process_improvement_intake_suppresses_recursion_for_process_changes(self) -> None:
        state = {
            "changeId": "process-producer-change",
            "cycle": 1,
            "contract": {
                "document": {
                    "affectedProjects": ["engineering-process"],
                    "source": "https://github.com/phuongnse/engineering-process/issues/214",
                }
            },
            "history": [
                {
                    "event": "evidence-invalidated",
                    "details": {"profile": "development", "reason": "input-digest-mismatch"},
                }
            ],
            "verification": {},
        }
        actor = {"actorId": "coordinator", "contextId": "finish", "kind": "agent"}

        results = process_improvement_intake(
            self.project_root,
            Path.cwd(),
            state,
            actor,
            search_tracker_fn=lambda repo, q: [],
            create_issue_fn=lambda repo, t, b: "https://github.com/phuongnse/engineering-process/issues/999",
        )

        self.assertEqual(1, len(results))
        self.assertEqual("suppressed", results[0]["status"])
        self.assertEqual("recursion-breaker", results[0]["reason"])

        events = [e["event"] for e in state["history"]]
        self.assertIn("process-improvement-suppressed", events)

    def test_process_improvement_intake_exhausts_budget(self) -> None:
        state = {
            "changeId": "consumer-change",
            "cycle": 1,
            "contract": {"document": {"affectedProjects": ["external-app"], "source": "https://example.com"}},
            "history": [
                {"event": "evidence-invalidated", "details": {"profile": "dev", "reason": "mismatch"}},
                {"event": "evidence-invalidated", "details": {"profile": "review", "reason": "mismatch"}},
            ],
            "verification": {},
        }
        actor = {"actorId": "coordinator", "contextId": "finish", "kind": "agent"}

        created = []
        def mock_create(repo: str, t: str, b: str) -> str:
            url = f"https://github.com/phuongnse/engineering-process/issues/{len(created) + 100}"
            created.append(url)
            return url

        results = process_improvement_intake(
            self.project_root,
            Path.cwd(),
            state,
            actor,
            search_tracker_fn=lambda repo, q: [],
            create_issue_fn=mock_create,
            max_issues_per_finish=1,
        )

        self.assertEqual(2, len(results))
        self.assertEqual("created", results[0]["status"])
        self.assertEqual("suppressed", results[1]["status"])
        self.assertEqual("budget-exhausted", results[1]["reason"])


if __name__ == "__main__":
    unittest.main()
