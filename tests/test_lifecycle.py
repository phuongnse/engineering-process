from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from engineering_process.commands import run_profile
from engineering_process.contracts import ProcessError, digest_json
from engineering_process.artifact_standards import resolve_standard
from engineering_process.lifecycle import (
    begin_implementation,
    finish_change,
    lifecycle_status,
    process_improvement_signals,
    register_plan,
    start_change,
    start_review,
    submit_review,
    verify_change,
)
from engineering_process.project import normalize_project
from engineering_process.production_engineering import load_invariant_floor
from engineering_process.repository import repository_snapshot
from engineering_process.source_publication import validate_current_source


PROCESS_ROOT = Path(__file__).resolve().parent.parent


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True, timeout=30)


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
        self.invariant_ids = [
            item["id"] for item in load_invariant_floor(PROCESS_ROOT)["invariants"]
        ]
        self.plan = {
            "schemaVersion": 5,
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
            "productionEngineering": [
                {
                    "id": invariant_id,
                    "applicability": "applicable",
                    "rationale": "The sample exercises this production boundary.",
                    "evidenceWorkItems": ["implementation"],
                }
                for invariant_id in self.invariant_ids
            ],
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

    def prepare_publication_candidate(self, branch: str = "fix/sample_change") -> str:
        base = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.root, text=True, timeout=30
        ).strip()
        self.project["lifecycle"]["publication"] = {"required": True}
        self.contract["comparisonBase"] = base
        self.plan["contractDigest"] = digest_json(self.contract)
        write_json(self.root / ".process" / "project.json", self.project)
        write_json(self.contract_path, self.contract)
        write_json(self.plan_path, self.plan)
        (self.root / "product.txt").write_text("publication candidate\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "fix: wire publication gate")
        git(self.root, "branch", "-M", branch)
        return base

    def approve_publication_candidate(self, *, moving_base: bool = False) -> str:
        base = self.prepare_publication_candidate()
        if moving_base:
            git(self.root, "branch", "publication-base", base)
            self.contract["comparisonBase"] = "publication-base"
            self.plan["contractDigest"] = digest_json(self.contract)
            write_json(self.contract_path, self.contract)
            write_json(self.plan_path, self.plan)
            git(self.root, "add", ".")
            git(self.root, "commit", "-qm", "fix: select comparison ref")
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
        write_json(review_path, self.review_document("approved"))
        state = submit_review(self.root, PROCESS_ROOT, "sample-change", review_path)
        self.assertEqual("approved", state["phase"])
        return base

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
                    "origin": "production-invariant",
                    "summary": "The bounded behavior is incorrect.",
                    "location": "product.txt",
                }
            ]
        return {
            "schemaVersion": 7,
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
            "productionEngineering": [
                {
                    "id": invariant_id,
                    "status": (
                        "violated"
                        if verdict == "changes-requested"
                        and invariant_id == "evidence-bound-assurance"
                        else "satisfied"
                    ),
                    "rationale": "The exact snapshot provides the required evidence.",
                    "evidence": (
                        []
                        if verdict == "changes-requested"
                        and invariant_id == "evidence-bound-assurance"
                        else ["development and review profiles"]
                    ),
                    **(
                        {"findingId": "bug"}
                        if verdict == "changes-requested"
                        and invariant_id == "evidence-bound-assurance"
                        else {}
                    ),
                }
                for invariant_id in self.invariant_ids
            ],
            "processImprovement": {
                "status": "none",
                "rationale": "No reusable process problem was observed.",
            },
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

    def test_start_freezes_the_base_without_rewriting_the_contract(self) -> None:
        initial = repository_snapshot(self.root)["head"]
        self.begin()
        (self.root / "product.txt").write_text("implementation\n", encoding="utf-8")
        git(self.root, "add", "product.txt")
        git(self.root, "commit", "-qm", "fix: implement the accepted change")
        self.verify_all()
        state = start_review(self.root, PROCESS_ROOT, "sample-change", actor_id="reviewer", context_id="review-context", kind="agent")
        self.assertNotEqual(initial, state["reviewAssignment"]["checkpoint"]["head"])
        self.assertEqual(initial, state["comparisonBaseCommit"])
        self.assertEqual({"digest": digest_json(self.contract), "document": self.contract}, state["contract"])

    def test_invalid_comparison_bases_do_not_create_a_run(self) -> None:
        for reference in ("missing-review-base", "HEAD:product.txt", "--help"):
            self.contract["comparisonBase"] = reference
            write_json(self.contract_path, self.contract)
            with self.subTest(reference=reference), self.assertRaises(ProcessError):
                start_change(self.root, PROCESS_ROOT, self.project, self.contract_path, actor_id="author", context_id="author-context", kind="agent")
            self.assertFalse((self.root / ".process/runs/sample-change/run.json").exists())

    def test_old_run_without_a_pinned_base_remains_readable(self) -> None:
        self.begin()
        path = self.root / ".process/runs/sample-change/run.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        del state["comparisonBaseCommit"]
        write_json(path, state)
        self.verify_all()
        state = start_review(self.root, PROCESS_ROOT, "sample-change", actor_id="reviewer", context_id="review-context", kind="agent")
        self.assertNotIn("comparisonBaseCommit", state)
        self.assertEqual("HEAD", state["contract"]["document"]["comparisonBase"])

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
        self.assertNotIn("publication", receipt)
        self.assertTrue(
            (self.root / ".process" / "receipts" / "sample-change.json").is_file()
        )
        self.assertIsNone(lifecycle_status(self.root, PROCESS_ROOT, "sample-change")["nextCommand"])

    def test_publication_opt_in_rejects_invalid_start_without_run(self) -> None:
        self.project["lifecycle"]["publication"] = {"required": True}
        with self.assertRaisesRegex(ProcessError, "publication branch validation failed"):
            start_change(
                self.root,
                PROCESS_ROOT,
                self.project,
                self.contract_path,
                actor_id="author",
                context_id="author-context",
                kind="agent",
            )
        self.assertFalse((self.root / ".process" / "runs" / "sample-change").exists())

    def test_publication_finish_rejects_invalid_source_and_keeps_approval(self) -> None:
        self.approve_publication_candidate()
        git(self.root, "branch", "-M", "codex/invalid")
        with self.assertRaisesRegex(ProcessError, "publication compatibility checks failed"):
            finish_change(
                self.root,
                PROCESS_ROOT,
                "sample-change",
                actor_id="coordinator",
                context_id="finish-context",
                kind="agent",
            )
        state = lifecycle_status(self.root, PROCESS_ROOT, "sample-change")
        self.assertEqual("approved", state["phase"])
        self.assertFalse(
            (self.root / ".process" / "receipts" / "sample-change.json").exists()
        )

    def test_publication_rejects_empty_range_before_profiles_then_committed_candidate_finishes(self) -> None:
        self.prepare_publication_candidate()
        self.contract["comparisonBase"] = "HEAD"
        self.plan["contractDigest"] = digest_json(self.contract)
        write_json(self.contract_path, self.contract)
        write_json(self.plan_path, self.plan)
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "fix: select initial boundary")
        self.begin()
        (self.root / "product.txt").write_text("corrected\n", encoding="utf-8")
        state_path = self.root / ".process/runs/sample-change/run.json"
        before = state_path.read_bytes()
        with patch("engineering_process.lifecycle.run_profile", wraps=run_profile) as runner:
            with self.assertRaisesRegex(ProcessError, "at least one commit.*before change verify"):
                verify_change(self.root, PROCESS_ROOT, self.project, "sample-change", "development")
            runner.assert_not_called()
        self.assertEqual(before, state_path.read_bytes())

        git(self.root, "add", "product.txt")
        git(self.root, "commit", "-qm", "fix: commit corrected candidate")
        self.verify_all()
        start_review(self.root, PROCESS_ROOT, "sample-change",
                     actor_id="reviewer", context_id="review-context", kind="agent")
        review_path = self.root / ".process/runs/review-input.json"
        write_json(review_path, self.review_document("approved"))
        submit_review(self.root, PROCESS_ROOT, "sample-change", review_path)
        state, receipt = finish_change(self.root, PROCESS_ROOT, "sample-change",
                                      actor_id="coordinator", context_id="finish-context", kind="agent")
        self.assertEqual(1, state["cycle"])
        self.assertEqual("completed", state["phase"])
        self.assertEqual("fix: commit corrected candidate", receipt["publication"]["subject"])

    def test_publication_verify_rejects_dirty_candidate_before_profiles(self) -> None:
        self.prepare_publication_candidate()
        self.begin()
        (self.root / "product.txt").write_text("not committed\n", encoding="utf-8")
        state_path = self.root / ".process/runs/sample-change/run.json"
        before = state_path.read_bytes()
        with patch("engineering_process.lifecycle.run_profile", wraps=run_profile) as runner:
            with self.assertRaisesRegex(ProcessError, "committed candidate changes"):
                verify_change(self.root, PROCESS_ROOT, self.project, "sample-change", "development")
            runner.assert_not_called()
        self.assertEqual(before, state_path.read_bytes())

    def test_publication_review_rechecks_branch_without_assigning_reviewer(self) -> None:
        self.prepare_publication_candidate()
        self.begin()
        self.verify_all()
        state_path = self.root / ".process/runs/sample-change/run.json"
        before = state_path.read_bytes()
        git(self.root, "branch", "-M", "codex/invalid")
        with self.assertRaisesRegex(ProcessError, "publication compatibility checks failed"):
            start_review(self.root, PROCESS_ROOT, "sample-change",
                         actor_id="reviewer", context_id="review-context", kind="agent")
        self.assertEqual(before, state_path.read_bytes())

    def test_publication_verify_rejects_head_mutation_during_preflight(self) -> None:
        self.prepare_publication_candidate()
        self.begin()
        state_path = self.root / ".process/runs/sample-change/run.json"
        before = state_path.read_bytes()

        def change_head(root: Path, base: str) -> dict:
            result = validate_current_source(root, base)
            git(root, "commit", "--allow-empty", "-qm", "fix: concurrent commit")
            return result

        with patch("engineering_process.lifecycle.validate_current_source", side_effect=change_head):
            with patch("engineering_process.lifecycle.run_profile", wraps=run_profile) as runner:
                with self.assertRaisesRegex(ProcessError, "repository changed while publication"):
                    verify_change(self.root, PROCESS_ROOT, self.project, "sample-change", "development")
                runner.assert_not_called()
        self.assertEqual(before, state_path.read_bytes())

    def test_publication_review_rejects_legacy_dirty_verification(self) -> None:
        self.prepare_publication_candidate()
        self.begin()
        (self.root / "product.txt").write_text("legacy uncommitted candidate\n", encoding="utf-8")
        # Reproduce evidence recorded by a process without the early preflight.
        with patch("engineering_process.lifecycle._publication_preflight"):
            self.verify_all()
        state_path = self.root / ".process/runs/sample-change/run.json"
        before = state_path.read_bytes()
        with self.assertRaisesRegex(ProcessError, "committed candidate changes"):
            start_review(self.root, PROCESS_ROOT, "sample-change",
                         actor_id="reviewer", context_id="review-context", kind="agent")
        self.assertEqual(before, state_path.read_bytes())

    def test_metadata_only_commit_still_invalidates_verification(self) -> None:
        self.prepare_publication_candidate()
        self.begin()
        self.verify_all()
        before = repository_snapshot(self.root)
        git(self.root, "commit", "--allow-empty", "-qm", "fix: metadata-only change")
        self.assertEqual(before["fingerprint"], repository_snapshot(self.root)["fingerprint"])
        with self.assertRaisesRegex(ProcessError, "verification evidence is stale or incomplete"):
            start_review(self.root, PROCESS_ROOT, "sample-change",
                         actor_id="reviewer", context_id="review-context", kind="agent")

    def test_publication_finish_records_validated_source_metadata(self) -> None:
        base = self.approve_publication_candidate(moving_base=True)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True, timeout=30).strip()
        git(self.root, "update-ref", "refs/heads/publication-base", head)
        state, receipt = finish_change(
            self.root,
            PROCESS_ROOT,
            "sample-change",
            actor_id="coordinator",
            context_id="finish-context",
            kind="agent",
        )
        self.assertEqual("completed", state["phase"])
        self.assertEqual(2, receipt["schemaVersion"])
        self.assertEqual(
            {
                "branch": "fix/sample_change",
                "subject": "fix: select comparison ref",
                "range": f"{base}..{head}",
            },
            receipt["publication"],
        )

    def test_publication_finish_rejects_branch_mutation_during_preflight(self) -> None:
        self.approve_publication_candidate()

        def change_branch(root: Path, base: str) -> dict:
            result = validate_current_source(root, base)
            git(root, "branch", "-M", "codex/changed-during-check")
            return result

        with patch("engineering_process.lifecycle.validate_current_source", side_effect=change_branch):
            with self.assertRaisesRegex(ProcessError, "repository changed while publication"):
                finish_change(self.root, PROCESS_ROOT, "sample-change",
                              actor_id="coordinator", context_id="finish-context", kind="agent")
        self.assertEqual("approved", lifecycle_status(self.root, PROCESS_ROOT, "sample-change")["phase"])
        self.assertFalse((self.root / ".process/receipts/sample-change.json").exists())

    def test_publication_finish_requires_a_pinned_base(self) -> None:
        self.approve_publication_candidate()
        state_path = self.root / ".process/runs/sample-change/run.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.pop("comparisonBaseCommit")
        write_json(state_path, state)
        with self.assertRaisesRegex(ProcessError, "requires a pinned comparison base"):
            finish_change(self.root, PROCESS_ROOT, "sample-change",
                          actor_id="coordinator", context_id="finish-context", kind="agent")
        self.assertEqual("approved", lifecycle_status(self.root, PROCESS_ROOT, "sample-change")["phase"])
        self.assertFalse((self.root / ".process/receipts/sample-change.json").exists())

    def test_publication_finish_rejects_transient_different_head(self) -> None:
        base = self.approve_publication_candidate()
        original = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True, timeout=30).strip()
        tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=self.root, text=True, timeout=30).strip()
        transient = subprocess.check_output(
            ["git", "commit-tree", tree, "-p", original, "-m", "fix: transient source"],
            cwd=self.root, text=True, timeout=30,
        ).strip()

        def validate_other_head(root: Path, comparison: str) -> dict:
            git(root, "update-ref", "HEAD", transient)
            try:
                return validate_current_source(root, comparison)
            finally:
                git(root, "update-ref", "HEAD", original)

        with patch("engineering_process.lifecycle.validate_current_source", side_effect=validate_other_head):
            with self.assertRaisesRegex(ProcessError, "does not match the candidate HEAD"):
                finish_change(self.root, PROCESS_ROOT, "sample-change",
                              actor_id="coordinator", context_id="finish-context", kind="agent")
        self.assertEqual(original, repository_snapshot(self.root)["head"])
        self.assertEqual([], validate_current_source(self.root, base)["issues"])
        self.assertEqual("approved", lifecycle_status(self.root, PROCESS_ROOT, "sample-change")["phase"])
        self.assertFalse((self.root / ".process/receipts/sample-change.json").exists())

    def test_failed_profile_persists_only_safe_diagnostic_metadata(self) -> None:
        self.begin()
        secret = "TOPSECRET-LIFECYCLE-VALUE"
        self.project["profiles"]["development"] = [
            {
                "id": "format",
                "run": [sys.executable, "-c", "raise SystemExit(0)"],
                "timeoutSeconds": 10,
            },
            {
                "id": "rust-tests",
                "run": [
                    sys.executable,
                    "-c",
                    f"import sys; print({secret!r}, file=sys.stderr); raise SystemExit(101)",
                ],
                "timeoutSeconds": 10,
            },
        ]
        state, report = verify_change(
            self.root, PROCESS_ROOT, self.project, "sample-change", "development"
        )
        self.assertEqual("implementing", state["phase"])
        self.assertEqual({"kind": "profile"}, report["scope"])
        self.assertEqual("rust-tests", report["diagnostic"]["check"])
        persisted = (
            self.root / ".process" / "runs" / "sample-change" / "run.json"
        ).read_text(encoding="utf-8")
        self.assertNotIn(secret, persisted)
        self.assertNotIn(sys.executable, persisted)

    def test_existing_run_without_diagnostic_fields_remains_readable(self) -> None:
        self.begin()
        verify_change(
            self.root, PROCESS_ROOT, self.project, "sample-change", "development"
        )
        run_path = self.root / ".process" / "runs" / "sample-change" / "run.json"
        run = json.loads(run_path.read_text(encoding="utf-8"))
        run["verification"]["development"].pop("scope")
        write_json(run_path, run)
        state = lifecycle_status(self.root, PROCESS_ROOT, "sample-change")
        self.assertEqual("implementing", state["phase"])

    def test_plan_registration_requires_every_canonical_invariant(self) -> None:
        start_change(
            self.root,
            PROCESS_ROOT,
            self.project,
            self.contract_path,
            actor_id="author",
            context_id="author-context",
            kind="agent",
        )
        invalid = deepcopy(self.plan)
        invalid["productionEngineering"].pop()
        write_json(self.plan_path, invalid)
        with self.assertRaisesRegex(ProcessError, "canonical invariants"):
            register_plan(
                self.root,
                PROCESS_ROOT,
                "sample-change",
                self.plan_path,
                actor_id="author",
                context_id="author-context",
                kind="agent",
            )
        self.assertEqual(
            "specified",
            lifecycle_status(self.root, PROCESS_ROOT, "sample-change")["phase"],
        )

    def test_new_run_rejects_the_legacy_plan_writer(self) -> None:
        start_change(
            self.root,
            PROCESS_ROOT,
            self.project,
            self.contract_path,
            actor_id="author",
            context_id="author-context",
            kind="agent",
        )
        legacy = deepcopy(self.plan)
        legacy["schemaVersion"] = 4
        legacy.pop("productionEngineering")
        write_json(self.plan_path, legacy)
        with self.assertRaisesRegex(ProcessError, "schemaVersion must be 5"):
            register_plan(
                self.root,
                PROCESS_ROOT,
                "sample-change",
                self.plan_path,
                actor_id="author",
                context_id="author-context",
                kind="agent",
            )

    def test_new_review_assignment_requires_version_seven_and_dispositions(self) -> None:
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
        self.assertEqual(7, state["reviewAssignment"]["reportSchemaVersion"])
        review_path = self.root / ".process" / "runs" / "review-input.json"
        review = self.review_document("approved")
        review["schemaVersion"] = 6
        review.pop("productionEngineering")
        review.pop("processImprovement")
        write_json(review_path, review)
        with self.assertRaisesRegex(ProcessError, "schemaVersion must be 7"):
            submit_review(self.root, PROCESS_ROOT, "sample-change", review_path)

        review = self.review_document("approved")
        review.pop("processImprovement")
        write_json(review_path, review)
        with self.assertRaisesRegex(ProcessError, "processImprovement"):
            submit_review(self.root, PROCESS_ROOT, "sample-change", review_path)

        review = self.review_document("approved")
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

    def test_shared_process_disposition_requires_a_durable_record_before_submit(self) -> None:
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
        review = self.review_document("approved")
        review["processImprovement"] = {
            "status": "shared-process",
            "rationale": "The consumer exposed a reusable process gap.",
        }
        write_json(review_path, review)
        with self.assertRaisesRegex(ProcessError, "recordUrl"):
            submit_review(self.root, PROCESS_ROOT, "sample-change", review_path)
        self.assertEqual(
            "review-pending",
            lifecycle_status(self.root, PROCESS_ROOT, "sample-change")["phase"],
        )

        review["processImprovement"]["recordUrl"] = (
            "https://github.com/phuongnse/engineering-process/issues/127"
        )
        write_json(review_path, review)
        state = submit_review(self.root, PROCESS_ROOT, "sample-change", review_path)
        self.assertEqual("approved", state["phase"])

    def test_legacy_plan_uses_version_six_review_evidence(self) -> None:
        start_change(
            self.root,
            PROCESS_ROOT,
            self.project,
            self.contract_path,
            actor_id="author",
            context_id="author-context",
            kind="agent",
        )
        run_path = self.root / ".process" / "runs" / "sample-change" / "run.json"
        run = json.loads(run_path.read_text(encoding="utf-8"))
        run.pop("requiredPlanSchemaVersion")
        write_json(run_path, run)
        legacy = deepcopy(self.plan)
        legacy["schemaVersion"] = 4
        legacy.pop("productionEngineering")
        write_json(self.plan_path, legacy)
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

        review = self.review_document("approved")
        review["schemaVersion"] = 6
        review.pop("productionEngineering")
        review.pop("processImprovement")
        review["findings"] = [self.non_blocking_finding()]
        review["findings"][0]["disposition"] = {
            "status": "resolved",
            "rationale": "Resolved in the reviewed snapshot.",
        }
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

    def test_unversioned_legacy_assignment_accepts_version_five_review(self) -> None:
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
        review.pop("productionEngineering")
        review.pop("processImprovement")
        review["findings"] = [self.non_blocking_finding()]
        review_path = self.root / ".process" / "runs" / "review-input.json"
        write_json(review_path, review)
        state = submit_review(self.root, PROCESS_ROOT, "sample-change", review_path)
        self.assertEqual("approved", state["phase"])

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
        self.assertEqual(
            ["review-changes-requested"], process_improvement_signals(state)
        )
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

    def test_resolved_invariant_finishes_with_its_identity_and_history_preserved(self) -> None:
        self.begin()
        self.verify_all()
        start_review(self.root, PROCESS_ROOT, "sample-change", actor_id="reviewer", context_id="review-context", kind="agent")
        first = self.review_document("changes-requested")
        first["findings"][0]["priority"] = "P2"
        first_path = self.root / ".process" / "runs" / "first-review.json"
        write_json(first_path, first)
        state = submit_review(self.root, PROCESS_ROOT, "sample-change", first_path)
        self.assertEqual("changes-requested", state["phase"])

        begin_implementation(self.root, PROCESS_ROOT, "sample-change", actor_id="implementer", context_id="implementation-context-2", kind="agent")
        (self.root / "product.txt").write_text("corrected\n", encoding="utf-8")
        self.verify_all()
        start_review(self.root, PROCESS_ROOT, "sample-change", actor_id="reviewer", context_id="review-context", kind="agent")
        approved = self.review_document("approved")
        carried = deepcopy(first["findings"][0])
        carried["severity"] = "non-blocking"
        carried["disposition"] = {"status": "resolved", "rationale": "The corrected candidate satisfies the invariant with fresh verification."}
        approved["findings"] = [carried]
        second_path = self.root / ".process" / "runs" / "second-review.json"
        for field, changed in (("priority", "P3"), ("origin", "contract")):
            invalid = deepcopy(approved)
            invalid["findings"][0][field] = changed
            write_json(second_path, invalid)
            with self.subTest(field=field), self.assertRaisesRegex(ProcessError, "identity fields are immutable"):
                submit_review(self.root, PROCESS_ROOT, "sample-change", second_path)
        write_json(second_path, approved)
        state = submit_review(self.root, PROCESS_ROOT, "sample-change", second_path)
        self.assertEqual("approved", state["phase"])
        self.assertEqual(first, state["reviewHistory"][0]["document"])
        self.assertEqual(approved, state["reviewHistory"][1]["document"])
        state, receipt = finish_change(self.root, PROCESS_ROOT, "sample-change", actor_id="coordinator", context_id="finish-context", kind="agent")
        self.assertEqual("completed", state["phase"])
        self.assertEqual(2, state["cycle"])
        self.assertEqual("approved", receipt["review"]["verdict"])

    def test_open_blockers_cannot_disappear_or_be_waived_in_supported_reports(self) -> None:
        self.begin()
        self.verify_all()
        start_review(self.root, PROCESS_ROOT, "sample-change", actor_id="reviewer", context_id="review-context", kind="agent")
        first = self.review_document("approved")
        first["verdict"] = "changes-requested"
        blocker = deepcopy(self.review_document("changes-requested")["findings"][0])
        blocker["origin"] = "contract"
        first["findings"] = [blocker]
        report_path = self.root / ".process/runs/review-input.json"
        write_json(report_path, first)
        submit_review(self.root, PROCESS_ROOT, "sample-change", report_path)
        begin_implementation(self.root, PROCESS_ROOT, "sample-change", actor_id="implementer", context_id="implementation-context-2", kind="agent")
        self.verify_all()
        start_review(self.root, PROCESS_ROOT, "sample-change", actor_id="reviewer", context_id="review-context", kind="agent")
        state_path = self.root / ".process/runs/sample-change/run.json"

        # The same closure rule applies to every supported report reader.
        for version in (5, 6, 7):
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["reviewAssignment"]["reportSchemaVersion"] = version
            write_json(state_path, state)
            for variant in ("omitted", "renamed", "accepted-risk", "tracked-follow-up"):
                report = self.review_document("approved")
                report["schemaVersion"] = version
                if version < 7:
                    del report["productionEngineering"]
                    del report["processImprovement"]
                finding = deepcopy(blocker)
                finding["severity"] = "non-blocking"
                finding["disposition"] = {
                    "status": "resolved" if variant == "renamed" else variant,
                    "rationale": "The previously reported behavior is still unimplemented.",
                    "owner": "maintainer",
                    "recordUrl": "https://example.invalid/issues/1",
                }
                if variant == "renamed":
                    finding["id"] = "renamed-bug"
                report["findings"] = [] if variant == "omitted" else [finding]
                write_json(report_path, report)
                with self.subTest(version=version, variant=variant), self.assertRaisesRegex(ProcessError, "prior blocking finding"):
                    submit_review(self.root, PROCESS_ROOT, "sample-change", report_path)
                self.assertEqual("review-pending", lifecycle_status(self.root, PROCESS_ROOT, "sample-change")["phase"])
                with self.assertRaisesRegex(ProcessError, "expected approved"):
                    finish_change(self.root, PROCESS_ROOT, "sample-change", actor_id="coordinator", context_id="finish", kind="agent")

        resolved = deepcopy(blocker)
        resolved["severity"] = "non-blocking"
        resolved["disposition"] = {"status": "resolved", "rationale": "Independent reinspection establishes why the criterion is satisfied."}
        report = self.review_document("approved")
        report["findings"] = [resolved]
        write_json(report_path, report)
        submit_review(self.root, PROCESS_ROOT, "sample-change", report_path)
        state, _receipt = finish_change(self.root, PROCESS_ROOT, "sample-change", actor_id="coordinator", context_id="finish", kind="agent")
        self.assertEqual("completed", state["phase"])

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

    def test_consumer_standard_change_invalidates_verified_profile(self) -> None:
        document = resolve_standard(None, PROCESS_ROOT, "pull-request").document
        definition = self.root / ".process" / "consumer-pr.json"
        write_json(definition, document)
        write_json(self.root / ".process" / "standards.json", {"schemaVersion": 1, "artifacts": {"pull-request": {"path": ".process/consumer-pr.json"}}})
        self.project["profiles"]["review"][0]["run"] = [
            sys.executable, str(PROCESS_ROOT / "processctl.py"), "artifact", "show",
            "--artifact", "pull-request", "--project-root", str(self.root), "--json",
        ]
        write_json(self.root / ".process" / "project.json", self.project)
        self.begin()
        self.verify_all()
        document["rules"]["sections"][0]["heading"] = "## Revised consumer requirements"
        write_json(definition, document)
        with self.assertRaisesRegex(ProcessError, "verification evidence is stale"):
            start_review(self.root, PROCESS_ROOT, "sample-change", actor_id="reviewer", context_id="review-context", kind="agent")

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
        self.assertEqual(["profile-failed"], process_improvement_signals(state))

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

    def test_live_producer_policy_requires_its_accepted_issue(self) -> None:
        project = json.loads((PROCESS_ROOT / ".process" / "project.json").read_text())
        self.project["lifecycle"]["processChanges"] = project["lifecycle"]["processChanges"]
        self.contract["consumerEvidence"] = [
            {"repository": "engineering-process", "incident": "Deferred issue handoff enablement."}
        ]
        for source in (
            "issue-110",
            "https://github.com/other/process/issues/110",
            "https://github.com/phuongnse/engineering-process/pull/118",
        ):
            with self.subTest(source=source):
                self.contract["source"] = source
                write_json(self.contract_path, self.contract)
                with self.assertRaisesRegex(ProcessError, "numbered issue"):
                    start_change(
                        self.root, PROCESS_ROOT, self.project, self.contract_path,
                        actor_id="author", context_id="author-context", kind="agent",
                    )
                self.assertFalse((self.root / ".process" / "runs").exists())
        self.contract["source"] = "https://github.com/phuongnse/engineering-process/issues/110"
        write_json(self.contract_path, self.contract)
        state = start_change(
            self.root, PROCESS_ROOT, self.project, self.contract_path,
            actor_id="author", context_id="author-context", kind="agent",
        )
        self.assertEqual("specified", state["phase"])

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
