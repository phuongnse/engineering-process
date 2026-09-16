"""Unit and contract tests for automated process-improvement incident intake."""

from __future__ import annotations

import json
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from engineering_process import VERSION
from engineering_process.contracts import ProcessError
from engineering_process.incidents import (
    CLOSED_TAXONOMY,
    Incident,
    _run_tracker_command,
    _default_search_tracker,
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
            "[consumer-process][org_2fmy-consumer][2.6.0][evidence-drop][evidence-integrity]",
            key,
        )
        self.assertNotEqual(
            stable_title_key("org/a-b", VERSION, "invariant", "kind"),
            stable_title_key("org-a/b", VERSION, "invariant", "kind"),
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
                "digest": "sha256:" + "1" * 64,
                "rationale": "C:\\private\\raw-details",
            },
        )
        body = render_sanitized_issue_body("test-org/test-repo", "2.6.0", incident, {})
        self.assertNotIn("ghp_secret12345", body)
        self.assertNotIn("private_key_abc", body)
        self.assertNotIn("raw-details", body)
        self.assertIn("development", body)
        self.assertIn("sha256:" + "1" * 64, body)

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
                "title": f"[consumer-process][consumer][{VERSION}][development][evidence-integrity] Verification issue",
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

    def test_process_issue_source_does_not_misclassify_a_consumer_change(self) -> None:
        state = {
            "contract": {
                "document": {
                    "affectedProjects": ["consumer-app"],
                    "source": "https://github.com/phuongnse/engineering-process/issues/214",
                }
            }
        }
        self.assertFalse(is_process_producer_change(state))

    def test_unsupported_issue_namespace_is_suppressed_without_default_fallback(self) -> None:
        project_path = self.project_root / ".process" / "project.json"
        project = json.loads(project_path.read_text(encoding="utf-8"))
        project["lifecycle"]["processChanges"]["acceptedIssueUrlPrefix"] = (
            "https://gitlab.example/process/issues/"
        )
        project_path.write_text(json.dumps(project), encoding="utf-8")
        state = {
            "changeId": "consumer-change",
            "cycle": 1,
            "contract": {"document": {"affectedProjects": ["external-app"], "source": "https://example.com"}},
            "history": [
                {"event": "evidence-invalidated", "details": {"profile": "development", "reason": "input-digest-mismatch"}},
            ],
            "verification": {},
        }
        actor = {"actorId": "coordinator", "contextId": "finish", "kind": "agent"}
        search = Mock()
        create = Mock()

        results = process_improvement_intake(
            self.project_root,
            Path.cwd(),
            state,
            actor,
            search_tracker_fn=search,
            create_issue_fn=create,
        )

        self.assertIsNone(resolve_process_repo(project))
        self.assertEqual("unsupported-tracker-namespace", results[0]["reason"])
        search.assert_not_called()
        create.assert_not_called()

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

    def test_budget_does_not_skip_reuse_for_a_later_incident(self) -> None:
        state = {
            "changeId": "consumer-change",
            "cycle": 1,
            "contract": {"document": {"affectedProjects": ["external-app"], "source": "https://example.com"}},
            "history": [
                {"event": "evidence-invalidated", "details": {"profile": "development", "reason": "input-digest-mismatch"}},
                {"event": "evidence-invalidated", "details": {"profile": "review", "reason": "input-digest-mismatch"}},
            ],
            "verification": {},
        }
        actor = {"actorId": "coordinator", "contextId": "finish", "kind": "agent"}
        review_key = stable_title_key("consumer", VERSION, "review", "evidence-integrity")
        queries: list[str] = []

        def search(repo: str, query: str) -> list[dict[str, str]]:
            queries.append(query)
            if query == review_key:
                return [{
                    "title": review_key + " existing",
                    "url": "https://github.com/phuongnse/engineering-process/issues/401",
                    "state": "CLOSED",
                }]
            return []

        create = Mock(return_value="https://github.com/phuongnse/engineering-process/issues/400")
        results = process_improvement_intake(
            self.project_root,
            Path.cwd(),
            state,
            actor,
            search_tracker_fn=search,
            create_issue_fn=create,
            max_issues_per_finish=1,
        )

        self.assertEqual(["created", "reused"], [item["status"] for item in results])
        self.assertEqual([review_key], [query for query in queries if query == review_key])
        create.assert_called_once()

    def test_tracker_search_uses_the_complete_identity(self) -> None:
        state = {
            "changeId": "consumer-change",
            "cycle": 1,
            "contract": {"document": {"affectedProjects": ["external-app"], "source": "https://example.com"}},
            "history": [
                {"event": "evidence-invalidated", "details": {"profile": "development", "reason": "input-digest-mismatch"}},
            ],
            "verification": {},
        }
        actor = {"actorId": "coordinator", "contextId": "finish", "kind": "agent"}
        queries: list[str] = []
        created = Mock(return_value="https://github.com/phuongnse/engineering-process/issues/300")

        results = process_improvement_intake(
            self.project_root,
            Path.cwd(),
            state,
            actor,
            search_tracker_fn=lambda repo, query: queries.append(query) or [
                {
                    "title": "[consumer-process][other-consumer][2.7.0][development][evidence-integrity] unrelated",
                    "url": "https://github.com/phuongnse/engineering-process/issues/301",
                    "state": "OPEN",
                }
            ],
            create_issue_fn=created,
        )

        expected = stable_title_key("consumer", VERSION, "development", "evidence-integrity")
        self.assertEqual([expected], queries)
        self.assertEqual("created", results[0]["status"])
        created.assert_called_once()
        self.assertTrue(created.call_args.args[1].startswith(expected + " "))

        second_search = Mock()
        second_create = Mock()
        second = process_improvement_intake(
            self.project_root,
            Path.cwd(),
            state,
            actor,
            search_tracker_fn=second_search,
            create_issue_fn=second_create,
        )
        self.assertEqual("reused", second[0]["status"])
        self.assertEqual(results[0]["recordUrl"], second[0]["recordUrl"])
        second_search.assert_not_called()
        second_create.assert_not_called()

    def test_search_failure_is_not_treated_as_no_match_and_is_idempotent(self) -> None:
        state = {
            "changeId": "consumer-change",
            "cycle": 1,
            "contract": {"document": {"affectedProjects": ["external-app"], "source": "https://example.com"}},
            "history": [
                {"event": "evidence-invalidated", "details": {"profile": "development", "reason": "input-digest-mismatch"}},
            ],
            "verification": {},
        }
        actor = {"actorId": "coordinator", "contextId": "finish", "kind": "agent"}
        search = Mock(side_effect=ProcessError("private tracker response"))
        create = Mock()

        first = process_improvement_intake(
            self.project_root,
            Path.cwd(),
            state,
            actor,
            search_tracker_fn=search,
            create_issue_fn=create,
        )
        second = process_improvement_intake(
            self.project_root,
            Path.cwd(),
            state,
            actor,
            search_tracker_fn=search,
            create_issue_fn=create,
        )

        self.assertEqual("failed", first[0]["status"])
        self.assertEqual("tracker-search-failed", first[0]["errorCode"])
        self.assertEqual(first, second)
        search.assert_called_once()
        create.assert_not_called()
        self.assertNotIn("private tracker response", json.dumps(state))

    def test_policy_disabled_collects_but_does_not_touch_tracker(self) -> None:
        project_path = self.project_root / ".process" / "project.json"
        project = json.loads(project_path.read_text(encoding="utf-8"))
        project["lifecycle"]["processChanges"].pop("acceptedIssueUrlPrefix")
        project_path.write_text(json.dumps(project), encoding="utf-8")
        state = {
            "changeId": "consumer-change",
            "cycle": 1,
            "contract": {"document": {"affectedProjects": ["external-app"], "source": "https://example.com"}},
            "history": [
                {"event": "evidence-invalidated", "details": {"profile": "development", "reason": "input-digest-mismatch"}},
            ],
            "verification": {},
        }
        actor = {"actorId": "coordinator", "contextId": "finish", "kind": "agent"}
        search = Mock()
        create = Mock()

        results = process_improvement_intake(
            self.project_root,
            Path.cwd(),
            state,
            actor,
            search_tracker_fn=search,
            create_issue_fn=create,
        )

        self.assertEqual("suppressed", results[0]["status"])
        self.assertEqual("policy-disabled", results[0]["reason"])
        search.assert_not_called()
        create.assert_not_called()
        self.assertIn("incident-collected", [event["event"] for event in state["history"]])

    def test_invalid_created_url_is_a_failure_without_created_evidence(self) -> None:
        state = {
            "changeId": "consumer-change",
            "cycle": 1,
            "contract": {"document": {"affectedProjects": ["external-app"], "source": "https://example.com"}},
            "history": [
                {"event": "evidence-invalidated", "details": {"profile": "development", "reason": "input-digest-mismatch"}},
            ],
            "verification": {},
        }
        actor = {"actorId": "coordinator", "contextId": "finish", "kind": "agent"}
        results = process_improvement_intake(
            self.project_root,
            Path.cwd(),
            state,
            actor,
            search_tracker_fn=lambda repo, query: [],
            create_issue_fn=lambda repo, title, body: "https://example.com/issues/300",
        )

        self.assertEqual("failed", results[0]["status"])
        self.assertEqual("tracker-create-failed", results[0]["errorCode"])
        self.assertNotIn("process-improvement-created", [event["event"] for event in state["history"]])

    @patch("engineering_process.incidents.process_supervisor")
    def test_default_tracker_search_is_bounded_and_queries_all_states(self, select_supervisor: Mock) -> None:
        process = Mock()
        process.stdout = io.BytesIO(b"[]")
        process.stderr = io.BytesIO()
        process.returncode = 0
        process.poll.return_value = 0
        process.wait.return_value = 0
        supervisor = Mock()
        supervisor.spawn.return_value = process
        supervisor.finalize.return_value = SimpleNamespace(bounded=True, error=None)
        select_supervisor.return_value = supervisor

        self.assertEqual([], _default_search_tracker("phuongnse/engineering-process", "STABLE-KEY"))
        command = supervisor.spawn.call_args.args[0]
        self.assertIn("--state", command)
        self.assertEqual("all", command[command.index("--state") + 1])
        self.assertIn("--limit", command)
        self.assertEqual("32", command[command.index("--limit") + 1])
        self.assertIn("STABLE-KEY in:title", command)

    @patch("engineering_process.incidents.process_supervisor")
    def test_default_tracker_search_failure_raises_without_returning_empty(self, select_supervisor: Mock) -> None:
        process = Mock()
        process.stdout = io.BytesIO(b"[]")
        process.stderr = io.BytesIO()
        process.returncode = 1
        process.poll.return_value = 1
        process.wait.return_value = 1
        supervisor = Mock()
        supervisor.spawn.return_value = process
        supervisor.finalize.return_value = SimpleNamespace(bounded=True, error=None)
        select_supervisor.return_value = supervisor

        with self.assertRaisesRegex(ProcessError, "tracker search failed"):
            _default_search_tracker("phuongnse/engineering-process", "STABLE-KEY")

    @patch("engineering_process.incidents.process_supervisor")
    def test_tracker_output_limit_terminates_before_unbounded_capture(self, select_supervisor: Mock) -> None:
        process = Mock()
        process.stdout = io.BytesIO(b"x" * 100)
        process.stderr = io.BytesIO()
        process.returncode = 0
        process.poll.return_value = 0
        process.wait.return_value = 0
        supervisor = Mock()
        supervisor.spawn.return_value = process
        supervisor.terminate.return_value = SimpleNamespace(bounded=True, error=None)
        supervisor.finalize.return_value = SimpleNamespace(bounded=True, error=None)
        select_supervisor.return_value = supervisor

        with self.assertRaisesRegex(ProcessError, "output limit"):
            _run_tracker_command(
                ["gh"], output_limit=8, failure_message="tracker search failed"
            )
        supervisor.terminate.assert_called_once()
        supervisor.finalize.assert_called_once()
        process.wait.assert_called()


if __name__ == "__main__":
    unittest.main()
