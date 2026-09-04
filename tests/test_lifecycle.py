from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from engineering_process.contracts import ProcessError, digest_json
from engineering_process.lifecycle import (
    begin_implementation,
    finish_change,
    lifecycle_status,
    register_plan,
    start_change,
    start_review,
    submit_review,
    verify_change,
)
from engineering_process.project import normalize_project
from engineering_process.repository import repository_snapshot


PROCESS_ROOT = Path(__file__).resolve().parent.parent


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "tests@example.invalid")
        git(self.root, "config", "user.name", "Tests")
        self.project = {
            "schemaVersion": 5,
            "project": "sample",
            "lifecycle": {"requiredProfiles": ["development", "review"]},
            "profiles": {
                "development": [
                    {
                        "id": "unit",
                        "run": [sys.executable, "-c", "raise SystemExit(0)"],
                        "timeoutSeconds": 10,
                    }
                ],
                "review": [
                    {
                        "id": "contract",
                        "run": [sys.executable, "-c", "raise SystemExit(0)"],
                        "timeoutSeconds": 10,
                    }
                ],
            },
        }
        write_json(self.root / ".process" / "project.json", self.project)
        (self.root / "product.txt").write_text("accepted\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "initial")
        self.contract = {
            "schemaVersion": 5,
            "id": "sample-change",
            "summary": "Make one sample change",
            "source": "issue-1",
            "comparisonBase": "HEAD",
            "risk": "low",
            "affectedProjects": ["sample"],
            "acceptanceCriteria": [
                {"id": "works", "outcome": "The accepted behavior works"}
            ],
            "requiredProfiles": ["development", "review"],
        }
        self.contract_path = self.root / "change.json"
        write_json(self.contract_path, self.contract)
        self.plan = {
            "schemaVersion": 4,
            "changeId": "sample-change",
            "contractDigest": digest_json(self.contract),
            "approach": "Make and verify the bounded change.",
            "workItems": [
                {
                    "id": "implementation",
                    "outcome": "Implement accepted behavior",
                    "affectedPaths": ["product.txt"],
                }
            ],
            "risks": [],
        }
        self.plan_path = self.root / "plan.json"
        write_json(self.plan_path, self.plan)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def begin(self) -> None:
        start_change(
            self.root,
            PROCESS_ROOT,
            self.project,
            self.contract_path,
            actor_id="author",
            context_id="author-context",
            kind="agent",
        )
        register_plan(
            self.root,
            PROCESS_ROOT,
            "sample-change",
            self.plan_path,
            actor_id="author",
            context_id="author-context",
            kind="agent",
        )
        begin_implementation(
            self.root,
            PROCESS_ROOT,
            "sample-change",
            actor_id="implementer",
            context_id="implementation-context",
            kind="agent",
        )

    def verify_all(self) -> None:
        verify_change(
            self.root, PROCESS_ROOT, self.project, "sample-change", "development"
        )
        state, report = verify_change(
            self.root, PROCESS_ROOT, self.project, "sample-change", "review"
        )
        self.assertEqual("passed", report["status"])
        self.assertEqual("verified", state["phase"])

    def review_document(self, verdict: str) -> dict[str, object]:
        checkpoint = repository_snapshot(self.root)
        findings = []
        if verdict == "changes-requested":
            findings = [
                {
                    "id": "bug",
                    "severity": "blocking",
                    "priority": "P1",
                    "criterionId": "works",
                    "origin": "contract",
                    "summary": "The bounded behavior is incorrect.",
                    "location": "product.txt",
                }
            ]
        return {
            "schemaVersion": 6,
            "changeId": "sample-change",
            "reviewer": {
                "actorId": "reviewer",
                "contextId": "review-context",
                "kind": "agent",
            },
            "checkpoint": checkpoint,
            "verdict": verdict,
            "summary": "Reviewed the accepted snapshot.",
            "findings": findings,
        }

    def non_blocking_finding(self) -> dict[str, object]:
        return {
            "id": "follow-up",
            "severity": "non-blocking",
            "priority": "P3",
            "criterionId": "works",
            "origin": "contract",
            "summary": "A bounded follow-up remains.",
            "location": "product.txt",
        }

    def test_happy_path_writes_one_completion_receipt(self) -> None:
        self.begin()
        self.verify_all()
        state = start_review(
            self.root,
            PROCESS_ROOT,
            "sample-change",
            actor_id="reviewer",
            context_id="review-context",
            kind="agent",
        )
        self.assertEqual("review-pending", state["phase"])
        review_path = self.root / ".process" / "runs" / "review-input.json"
        write_json(review_path, self.review_document("approved"))
        state = submit_review(
            self.root, PROCESS_ROOT, "sample-change", review_path
        )
        self.assertEqual("approved", state["phase"])
        state, receipt = finish_change(
            self.root,
            PROCESS_ROOT,
            "sample-change",
            actor_id="coordinator",
            context_id="finish-context",
            kind="agent",
        )
        self.assertEqual("completed", state["phase"])
        self.assertEqual("approved", receipt["review"]["verdict"])
        self.assertTrue(
            (self.root / ".process" / "receipts" / "sample-change.json").is_file()
        )
        self.assertIsNone(lifecycle_status(self.root, PROCESS_ROOT, "sample-change")["nextCommand"])

    def test_new_review_assignment_requires_version_six_dispositions(self) -> None:
        self.begin()
        self.verify_all()
        state = start_review(
            self.root,
            PROCESS_ROOT,
            "sample-change",
            actor_id="reviewer",
            context_id="review-context",
            kind="agent",
        )
        self.assertEqual(6, state["reviewAssignment"]["reportSchemaVersion"])
        review_path = self.root / ".process" / "runs" / "review-input.json"
        review = self.review_document("approved")
        review["schemaVersion"] = 5
        write_json(review_path, review)
        with self.assertRaisesRegex(ProcessError, "schemaVersion must be 6"):
            submit_review(self.root, PROCESS_ROOT, "sample-change", review_path)

        review["schemaVersion"] = 6
        review["findings"] = [self.non_blocking_finding()]
        write_json(review_path, review)
        with self.assertRaisesRegex(ProcessError, "disposition"):
            submit_review(self.root, PROCESS_ROOT, "sample-change", review_path)

        review["findings"][0]["disposition"] = {
            "status": "tracked-follow-up",
            "rationale": "The accepted behavior is complete; hardening is separate.",
            "owner": "process-owner",
            "recordUrl": "https://github.com/phuongnse/engineering-process/issues/111",
        }
        write_json(review_path, review)
        state = submit_review(self.root, PROCESS_ROOT, "sample-change", review_path)
        self.assertEqual("approved", state["phase"])

    def test_legacy_review_assignment_and_version_five_evidence_can_finish(self) -> None:
        self.begin()
        self.verify_all()
        start_review(
            self.root,
            PROCESS_ROOT,
            "sample-change",
            actor_id="reviewer",
            context_id="review-context",
            kind="agent",
        )
        run_path = self.root / ".process" / "runs" / "sample-change" / "run.json"
        run = json.loads(run_path.read_text(encoding="utf-8"))
        run["reviewAssignment"].pop("reportSchemaVersion")
        write_json(run_path, run)

        review = self.review_document("approved")
        review["schemaVersion"] = 5
        review["findings"] = [self.non_blocking_finding()]
        review_path = self.root / ".process" / "runs" / "review-input.json"
        write_json(review_path, review)
        state = submit_review(self.root, PROCESS_ROOT, "sample-change", review_path)
        self.assertEqual("approved", state["phase"])
        state, _ = finish_change(
            self.root,
            PROCESS_ROOT,
            "sample-change",
            actor_id="coordinator",
            context_id="finish-context",
            kind="agent",
        )
        self.assertEqual("completed", state["phase"])

    def test_self_review_rejects_actor_or_context_reuse(self) -> None:
        self.begin()
        self.verify_all()
        with self.assertRaisesRegex(ProcessError, "reviewer actor"):
            start_review(
                self.root,
                PROCESS_ROOT,
                "sample-change",
                actor_id="implementer",
                context_id="new-context",
                kind="agent",
            )
        with self.assertRaisesRegex(ProcessError, "reviewer context"):
            start_review(
                self.root,
                PROCESS_ROOT,
                "sample-change",
                actor_id="different-actor",
                context_id="implementation-context",
                kind="agent",
            )

    def test_every_delegated_implementation_participant_blocks_review(self) -> None:
        self.begin()
        state = begin_implementation(
            self.root,
            PROCESS_ROOT,
            "sample-change",
            actor_id="delegate",
            context_id="delegate-context",
            kind="agent",
        )
        self.assertEqual(2, len(state["implementations"]))
        self.verify_all()
        with self.assertRaisesRegex(ProcessError, "reviewer actor"):
            start_review(
                self.root,
                PROCESS_ROOT,
                "sample-change",
                actor_id="delegate",
                context_id="fresh-review-context",
                kind="agent",
            )
        with self.assertRaisesRegex(ProcessError, "reviewer context"):
            start_review(
                self.root,
                PROCESS_ROOT,
                "sample-change",
                actor_id="fresh-reviewer",
                context_id="delegate-context",
                kind="agent",
            )
    def test_stale_verification_blocks_review(self) -> None:
        self.begin()
        self.verify_all()
        (self.root / "product.txt").write_text("changed after verification\n", encoding="utf-8")
        with self.assertRaisesRegex(ProcessError, "stale or incomplete"):
            start_review(
                self.root,
                PROCESS_ROOT,
                "sample-change",
                actor_id="reviewer",
                context_id="review-context",
                kind="agent",
            )

    def test_source_change_after_verification_reopens_a_new_cycle(self) -> None:
        self.begin()
        self.verify_all()
        with self.assertRaisesRegex(ProcessError, "current verified evidence"):
            begin_implementation(
                self.root,
                PROCESS_ROOT,
                "sample-change",
                actor_id="implementer",
                context_id="implementation-context-2",
                kind="agent",
            )
        (self.root / "product.txt").write_text("new implementation\n", encoding="utf-8")
        state = begin_implementation(
            self.root,
            PROCESS_ROOT,
            "sample-change",
            actor_id="implementer",
            context_id="implementation-context-2",
            kind="agent",
        )
        self.assertEqual(2, state["cycle"])
        self.assertEqual("implementing", state["phase"])

    def test_requested_changes_start_a_new_cycle(self) -> None:
        self.begin()
        self.verify_all()
        start_review(
            self.root,
            PROCESS_ROOT,
            "sample-change",
            actor_id="reviewer",
            context_id="review-context",
            kind="agent",
        )
        review_path = self.root / ".process" / "runs" / "review-input.json"
        write_json(review_path, self.review_document("changes-requested"))
        state = submit_review(self.root, PROCESS_ROOT, "sample-change", review_path)
        self.assertEqual("changes-requested", state["phase"])
        state = begin_implementation(
            self.root,
            PROCESS_ROOT,
            "sample-change",
            actor_id="implementer",
            context_id="implementation-context-2",
            kind="agent",
        )
        self.assertEqual(2, state["cycle"])
        self.assertEqual("implementing", state["phase"])
        self.assertEqual({}, state["verification"])

    def test_correction_review_requires_the_original_reviewer_identity(self) -> None:
        self.begin()
        self.verify_all()
        start_review(
            self.root,
            PROCESS_ROOT,
            "sample-change",
            actor_id="reviewer",
            context_id="review-context",
            kind="agent",
        )
        review_path = self.root / ".process" / "runs" / "review-input.json"
        write_json(review_path, self.review_document("changes-requested"))
        submit_review(self.root, PROCESS_ROOT, "sample-change", review_path)
        begin_implementation(
            self.root,
            PROCESS_ROOT,
            "sample-change",
            actor_id="implementer",
            context_id="implementation-context-2",
            kind="agent",
        )
        self.verify_all()

        with self.assertRaisesRegex(ProcessError, "original independent reviewer"):
            start_review(
                self.root,
                PROCESS_ROOT,
                "sample-change",
                actor_id="replacement-reviewer",
                context_id="replacement-context",
                kind="agent",
            )

        state = start_review(
            self.root,
            PROCESS_ROOT,
            "sample-change",
            actor_id="reviewer",
            context_id="review-context",
            kind="agent",
        )
        self.assertEqual("review-pending", state["phase"])

    def test_third_changes_requested_review_blocks_without_bypassing_review(self) -> None:
        self.begin()
        review_path = self.root / ".process" / "runs" / "review-input.json"
        for attempt in range(1, 4):
            self.verify_all()
            start_review(
                self.root,
                PROCESS_ROOT,
                "sample-change",
                actor_id="reviewer",
                context_id="review-context",
                kind="agent",
            )
            write_json(review_path, self.review_document("changes-requested"))
            state = submit_review(
                self.root, PROCESS_ROOT, "sample-change", review_path
            )
            if attempt < 3:
                self.assertEqual("changes-requested", state["phase"])
                begin_implementation(
                    self.root,
                    PROCESS_ROOT,
                    "sample-change",
                    actor_id="implementer",
                    context_id=f"implementation-context-{attempt + 1}",
                    kind="agent",
                )
        self.assertEqual("blocked", state["phase"])
        with self.assertRaisesRegex(ProcessError, "expected planned, changes-requested"):
            begin_implementation(
                self.root,
                PROCESS_ROOT,
                "sample-change",
                actor_id="implementer",
                context_id="implementation-context-4",
                kind="agent",
            )

    def test_failed_profile_does_not_advance(self) -> None:
        self.project["profiles"]["development"][0]["run"] = [
            sys.executable,
            "-c",
            "raise SystemExit(9)",
        ]
        self.begin()
        state, report = verify_change(
            self.root, PROCESS_ROOT, self.project, "sample-change", "development"
        )
        self.assertEqual("failed", report["status"])
        self.assertEqual("implementing", state["phase"])

    def test_process_change_policy_requires_consumer_evidence(self) -> None:
        self.project["lifecycle"]["processChanges"] = {
            "requireConsumerEvidence": True
        }
        with self.assertRaisesRegex(ProcessError, "real consumer incident"):
            start_change(
                self.root,
                PROCESS_ROOT,
                self.project,
                self.contract_path,
                actor_id="author",
                context_id="author-context",
                kind="agent",
            )

    def test_process_change_policy_requires_accepted_issue_url(self) -> None:
        prefix = "https://github.com/example/process/issues/"
        self.project["lifecycle"]["processChanges"] = {
            "requireConsumerEvidence": True,
            "acceptedIssueUrlPrefix": prefix,
        }
        self.contract["consumerEvidence"] = [
            {"repository": "consumer", "incident": "A shared invariant failed."}
        ]
        invalid_sources = (
            "issue-1",
            "https://github.com/other/process/issues/1",
            prefix,
            prefix + "0",
            prefix + "01",
            prefix + "1?state=open",
            prefix + "1#comment",
        )
        for source in invalid_sources:
            with self.subTest(source=source):
                self.contract["source"] = source
                write_json(self.contract_path, self.contract)
                with self.assertRaisesRegex(ProcessError, "numbered issue"):
                    start_change(
                        self.root,
                        PROCESS_ROOT,
                        self.project,
                        self.contract_path,
                        actor_id="author",
                        context_id="author-context",
                        kind="agent",
                    )
                self.assertFalse(
                    (self.root / ".process" / "runs" / "sample-change").exists()
                )

        self.contract["source"] = prefix + "42"
        write_json(self.contract_path, self.contract)
        state = start_change(
            self.root,
            PROCESS_ROOT,
            self.project,
            self.contract_path,
            actor_id="author",
            context_id="author-context",
            kind="agent",
        )
        self.assertEqual("specified", state["phase"])

    def test_process_change_policy_rejects_malformed_issue_prefix(self) -> None:
        self.contract["consumerEvidence"] = [
            {"repository": "consumer", "incident": "A shared invariant failed."}
        ]
        invalid_prefixes = (
            "https:////",
            "http://github.com/example/process/issues/",
            "https://-github.com/example/process/issues/",
            "https://github.com/example/process/issues/?state=open",
            "https://github.com/example/process/issues/\n",
        )
        for prefix in invalid_prefixes:
            with self.subTest(prefix=prefix):
                self.contract["source"] = prefix + "42"
                write_json(self.contract_path, self.contract)
                self.project["lifecycle"]["processChanges"] = {
                    "requireConsumerEvidence": True,
                    "acceptedIssueUrlPrefix": prefix,
                }
                with self.assertRaises(ProcessError):
                    project = normalize_project(self.project, PROCESS_ROOT)
                    start_change(
                        self.root,
                        PROCESS_ROOT,
                        project,
                        self.contract_path,
                        actor_id="author",
                        context_id="author-context",
                        kind="agent",
                    )
                self.assertFalse(
                    (self.root / ".process" / "runs" / "sample-change").exists()
                )

    def test_evidence_only_process_change_policy_remains_compatible(self) -> None:
        self.project["lifecycle"]["processChanges"] = {
            "requireConsumerEvidence": True
        }
        self.contract["consumerEvidence"] = [
            {"repository": "consumer", "incident": "A shared invariant failed."}
        ]
        write_json(self.contract_path, self.contract)
        state = start_change(
            self.root,
            PROCESS_ROOT,
            self.project,
            self.contract_path,
            actor_id="author",
            context_id="author-context",
            kind="agent",
        )
        self.assertEqual("specified", state["phase"])


if __name__ == "__main__":
    unittest.main()
