from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from engineering_process.contracts import ProcessError, digest_json, read_json, validate_document
from engineering_process.lifecycle import (
    begin_implementation, finish_change, register_plan, start_change,
    start_review, submit_review, verify_change,
)
from engineering_process.repository import repository_snapshot
from engineering_process import lifecycle
from engineering_process import review_contexts
from tests import test_lifecycle as fixture_module
from tests.test_lifecycle import PROCESS_ROOT, git, write_json


class ReviewContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixture_module.LifecycleTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root.resolve()
        self.prepare("current")

    def state_path(self, change: str, root: Path | None = None) -> Path:
        return (root or self.root) / ".process" / "runs" / change / "run.json"

    def prepare(self, change: str, root: Path | None = None) -> dict:
        root = root or self.root
        contract = deepcopy(self.fixture.contract)
        contract["id"] = change
        folder = self.state_path(change, root).parent
        source = folder / "change-input.json"
        write_json(source, contract)
        start_change(root, PROCESS_ROOT, self.fixture.project, source,
                     actor_id="author", context_id=f"author-{change}", kind="agent")
        plan = deepcopy(self.fixture.plan)
        plan.update(changeId=change, contractDigest=digest_json(contract))
        plan_path = folder / "plan-input.json"
        write_json(plan_path, plan)
        register_plan(root, PROCESS_ROOT, change, plan_path,
                      actor_id="author", context_id=f"author-{change}", kind="agent")
        begin_implementation(root, PROCESS_ROOT, change, actor_id="implementer",
                             context_id=f"implementation-{change}", kind="agent")
        for profile in contract["requiredProfiles"]:
            state, report = verify_change(root, PROCESS_ROOT, self.fixture.project, change, profile)
            self.assertEqual("passed", report["status"])
        return state

    def assign(self, change: str, context: str = "review-context", *,
               root: Path | None = None, actor: str = "reviewer", replace: bool = False) -> dict:
        return start_review(root or self.root, PROCESS_ROOT, change, actor_id=actor,
                            context_id=context, kind="agent", replace_reused=replace)

    def reopen(self, change: str) -> None:
        product = self.root / "product.txt"
        original = product.read_bytes()
        try:
            product.write_bytes(b"new implementation cycle\n")
            begin_implementation(self.root, PROCESS_ROOT, change, actor_id="implementer",
                                 context_id=f"implementation-{change}", kind="agent")
        finally:
            product.write_bytes(original)

    def report(self, change: str, *, root: Path | None = None) -> Path:
        root = root or self.root
        state = read_json(self.state_path(change, root))
        report = self.fixture.review_document("approved")
        report.update(changeId=change, reviewer=state["reviewAssignment"]["reviewer"],
                      checkpoint=repository_snapshot(root))
        path = self.state_path(change, root).with_name("review-input.json")
        write_json(path, report)
        return path

    def historical_reuse(self) -> dict:
        self.prepare("previous")
        self.assign("previous")
        # An old process version admitted this second assignment. Preserve real
        # canonical transition output instead of inventing historical run shapes.
        with patch("engineering_process.lifecycle.require_unreused_context"):
            return self.assign("current")

    def sibling(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "linked worktree"
        self.assertTrue(root.resolve().is_relative_to(Path(temporary.name).resolve()))
        git(self.root, "worktree", "add", "--detach", str(root), "HEAD")
        self.addCleanup(lambda: git(self.root, "worktree", "remove", "--force", str(root)))
        return root

    def test_different_change_rejects_context_even_with_another_actor(self) -> None:
        self.prepare("previous")
        self.assign("previous")
        before = self.state_path("current").read_bytes()
        with self.assertRaisesRegex(ProcessError, "used by another change"):
            self.assign("current", actor="renamed-reviewer")
        self.assertEqual(before, self.state_path("current").read_bytes())

    def test_foreign_implementation_context_is_not_a_fresh_reviewer(self) -> None:
        self.prepare("previous")
        with self.assertRaisesRegex(ProcessError, "used by another change"):
            self.assign("current", "implementation-previous")

    def test_sibling_worktree_and_contract_identity_are_checked(self) -> None:
        sibling = self.sibling()
        self.prepare("previous", sibling)
        self.assign("previous", root=sibling)
        with self.assertRaisesRegex(ProcessError, "used by another change"):
            self.assign("current")

        original = read_json(self.state_path("previous", sibling))
        same = deepcopy(original)
        same["changeId"] = "current"
        same["contract"] = read_json(self.state_path("current"))["contract"]
        write_json(self.state_path("current", sibling), same)
        self.state_path("previous", sibling).unlink()
        self.assign("current")  # A copy of the same accepted change is not foreign.
        same["contract"]["digest"] = "sha256:" + "a" * 64
        write_json(self.state_path("current", sibling), same)
        with self.assertRaisesRegex(ProcessError, "used by another change"):
            submit_review(self.root, PROCESS_ROOT, "current", self.report("current"))

    def test_interrupted_assignment_remains_visible_in_history(self) -> None:
        self.prepare("previous")
        self.assign("previous")
        product = self.root / "product.txt"
        original = product.read_bytes()
        product.write_bytes(b"correction\n")
        begin_implementation(self.root, PROCESS_ROOT, "previous", actor_id="implementer",
                             context_id="implementation-previous", kind="agent")
        product.write_bytes(original)
        self.assertIsNone(read_json(self.state_path("previous"))["reviewAssignment"])
        with self.assertRaisesRegex(ProcessError, "used by another change"):
            self.assign("current")

    def test_replacement_preserves_cycle_evidence_and_rejects_old_report(self) -> None:
        before = self.historical_reuse()
        old_report = self.report("current")
        replacement = self.assign("current", "fresh-context", actor="fresh-reviewer", replace=True)
        for key in ("phase", "cycle", "contract", "plan", "comparisonBaseCommit", "verification",
                    "implementations", "currentImplementation", "review", "reviewHistory", "receipt"):
            self.assertEqual(before[key], replacement[key], key)
        self.assertEqual(before["history"], replacement["history"][:-1])
        event = replacement["history"][-1]
        self.assertEqual("review-assignment-replaced", event["event"])
        self.assertEqual(before["reviewAssignment"], event["details"]["previousAssignment"])
        self.assertEqual(replacement["reviewAssignment"], event["details"]["replacementAssignment"])
        self.assertEqual("previous", event["details"]["conflict"]["changeId"])
        self.assertEqual(before["reviewAssignment"]["checkpoint"], replacement["reviewAssignment"]["checkpoint"])
        with self.assertRaisesRegex(ProcessError, "does not match the assigned"):
            submit_review(self.root, PROCESS_ROOT, "current", old_report)
        submit_review(self.root, PROCESS_ROOT, "current", self.report("current"))
        state, receipt = finish_change(self.root, PROCESS_ROOT, "current", actor_id="author",
                                       context_id="author-current", kind="agent")
        self.assertEqual("completed", state["phase"])
        self.assertEqual("fresh-context", receipt["review"]["reviewer"]["contextId"])

    def test_valid_pending_assignment_cannot_be_shopped_for_another_reviewer(self) -> None:
        self.assign("current")
        before = self.state_path("current").read_bytes()
        with self.assertRaisesRegex(ProcessError, "no recorded cross-change reuse"):
            self.assign("current", "different", replace=True)
        self.assertEqual(before, self.state_path("current").read_bytes())

    def test_replacement_rejects_reused_or_implementation_identity_without_mutation(self) -> None:
        self.historical_reuse()
        before = self.state_path("current").read_bytes()
        for actor, context in (("renamed", "review-context"), ("implementer", "fresh"),
                               ("new", "implementation-current")):
            with self.subTest(actor=actor, context=context), self.assertRaises(ProcessError):
                self.assign("current", context, actor=actor, replace=True)
            self.assertEqual(before, self.state_path("current").read_bytes())

    def test_replacement_rejects_stale_snapshot_or_an_existing_report(self) -> None:
        self.historical_reuse()
        before = self.state_path("current").read_bytes()
        product = self.root / "product.txt"
        original = product.read_bytes()
        product.write_bytes(b"changed\n")
        with self.assertRaisesRegex(ProcessError, "stale or incomplete"):
            self.assign("current", "fresh", replace=True)
        product.write_bytes(original)
        report = self.state_path("current").with_name("review-1.json")
        report.write_text("already authored\n", encoding="utf-8")
        with self.assertRaisesRegex(ProcessError, "report already exists"):
            self.assign("current", "fresh", replace=True)
        self.assertEqual(before, self.state_path("current").read_bytes())

    def test_previously_submitted_review_cannot_use_replacement(self) -> None:
        self.assign("current")
        submit_review(self.root, PROCESS_ROOT, "current", self.report("current"))
        before = self.state_path("current").read_bytes()
        with self.assertRaisesRegex(ProcessError, "expected review-pending"):
            self.assign("current", "fresh", replace=True)
        self.assertEqual(before, self.state_path("current").read_bytes())

    def test_submit_and_finish_reject_legacy_reused_assignment(self) -> None:
        self.historical_reuse()
        report = self.report("current")
        with self.assertRaisesRegex(ProcessError, "used by another change"):
            submit_review(self.root, PROCESS_ROOT, "current", report)
        with patch("engineering_process.lifecycle.require_unreused_context"):
            submit_review(self.root, PROCESS_ROOT, "current", report)
        before = self.state_path("current").read_bytes()
        with self.assertRaisesRegex(ProcessError, "used by another change"):
            finish_change(self.root, PROCESS_ROOT, "current", actor_id="author",
                          context_id="author-current", kind="agent")
        self.assertEqual(before, self.state_path("current").read_bytes())

    def test_schema_owns_replacement_event_shape(self) -> None:
        self.historical_reuse()
        state = self.assign("current", "fresh", replace=True)
        for field in ("reason", "previousAssignment", "replacementAssignment", "conflict"):
            invalid = deepcopy(state)
            del invalid["history"][-1]["details"][field]
            with self.subTest(field=field), self.assertRaises(ProcessError):
                validate_document(invalid, "run", schema_root=PROCESS_ROOT / "schemas")

    def test_invalid_or_future_history_schema_fails_without_assignment(self) -> None:
        self.prepare("previous")
        state = read_json(self.state_path("previous"))
        state["schemaVersion"] = 999
        write_json(self.state_path("previous"), state)
        before = self.state_path("current").read_bytes()
        with self.assertRaises(ProcessError):
            self.assign("current", "fresh")
        self.assertEqual(before, self.state_path("current").read_bytes())

    def test_scan_budgets_fail_without_mutation(self) -> None:
        before = self.state_path("current").read_bytes()
        for name, limit in (("MAX_WORKTREES", 0), ("MAX_RUN_ENTRIES", 0),
                            ("MAX_TOTAL_BYTES", 1), ("MAX_JSON_BYTES", 1), ("MAX_GIT_BYTES", 1),
                            ("SCAN_TIMEOUT_SECONDS", 0)):
            with self.subTest(limit=name), patch.object(review_contexts, name, limit):
                expected = "exceeds 1 bytes" if name == "MAX_JSON_BYTES" else "limit"
                with self.assertRaisesRegex(ProcessError, expected):
                    self.assign("current", "fresh")
            self.assertEqual(before, self.state_path("current").read_bytes())

    def test_conflict_order_does_not_depend_on_directory_enumeration(self) -> None:
        for change in ("zulu", "alpha"):
            self.prepare(change)
            with patch("engineering_process.lifecycle.require_unreused_context"):
                self.assign(change)
        original = os.scandir

        @contextmanager
        def reversed_scan(path):
            with original(path) as entries:
                children = sorted(entries, key=lambda entry: entry.name, reverse=True)
            yield iter(children)

        current = read_json(self.state_path("current"))
        with patch.object(os, "scandir", reversed_scan):
            conflict = review_contexts.recorded_context_conflict(
                self.root, PROCESS_ROOT, current, "review-context"
            )
        self.assertEqual("alpha", conflict["changeId"])

    def test_scan_deadline_includes_every_git_probe(self) -> None:
        original = review_contexts._git
        before = self.state_path("current").read_bytes()
        # The first probe belongs to lock acquisition, outside the scan budget.
        for expired_probe in (2, 3, 4, 5):
            now = calls = 0

            def expire(root, arguments, *, deadline=None):
                nonlocal now, calls
                calls += 1
                if calls == expired_probe:
                    now = review_contexts.SCAN_TIMEOUT_SECONDS
                return original(root, arguments, deadline=deadline)

            with self.subTest(probe=expired_probe), patch.object(
                review_contexts, "_git", expire
            ), patch.object(review_contexts.time, "monotonic", lambda: now):
                with self.assertRaisesRegex(ProcessError, "time limit"):
                    self.assign("current", "fresh")
            self.assertEqual(expired_probe, calls)
            self.assertEqual(before, self.state_path("current").read_bytes())

    def test_expired_history_read_cannot_return_a_conflict(self) -> None:
        self.prepare("previous")
        self.assign("previous")
        original = review_contexts._read_run
        now = 0

        def expire(path, parents, process_root):
            nonlocal now
            result = original(path, parents, process_root)
            if path == self.state_path("previous"):
                now = review_contexts.SCAN_TIMEOUT_SECONDS
            return result

        before = self.state_path("current").read_bytes()
        with patch.object(review_contexts, "_read_run", expire), patch.object(
            review_contexts.time, "monotonic", lambda: now
        ):
            with self.assertRaisesRegex(ProcessError, "time limit"):
                self.assign("current")
        self.assertEqual(before, self.state_path("current").read_bytes())

    def test_linked_history_is_rejected(self) -> None:
        self.prepare("previous")
        path = self.state_path("previous")
        backup = path.with_name("saved.json")
        path.rename(backup)
        try:
            path.symlink_to(backup)
        except OSError as error:
            backup.rename(path)
            self.skipTest(f"symlink privilege unavailable: {error}")
        with self.assertRaisesRegex(ProcessError, "link or reparse"):
            self.assign("current", "fresh")

    def test_inaccessible_registered_worktree_fails(self) -> None:
        sibling = self.sibling()
        original = review_contexts._safe_info

        def inaccessible(path: Path, *, directory: bool):
            if path.resolve() == sibling.resolve():
                raise PermissionError("registered worktree inaccessible")
            return original(path, directory=directory)

        before = self.state_path("current").read_bytes()
        with patch.object(review_contexts, "_safe_info", inaccessible):
            with self.assertRaisesRegex(ProcessError, "inaccessible"):
                self.assign("current", "fresh")
        self.assertEqual(before, self.state_path("current").read_bytes())

    def test_disappearing_run_is_not_treated_as_absent_history(self) -> None:
        self.prepare("previous")
        original = review_contexts._read_run

        def disappear(path: Path, parents: list[Path], process_root: Path):
            if path == self.state_path("previous"):
                raise FileNotFoundError("run disappeared during inspection")
            return original(path, parents, process_root)

        before = self.state_path("current").read_bytes()
        with patch.object(review_contexts, "_read_run", disappear):
            with self.assertRaisesRegex(ProcessError, "disappeared"):
                self.assign("current", "fresh")
        self.assertEqual(before, self.state_path("current").read_bytes())

    def test_windows_reparse_history_is_rejected(self) -> None:
        from types import SimpleNamespace
        import stat

        self.prepare("previous")
        target = self.state_path("previous")
        original = Path.lstat

        def reparse(path: Path):
            info = original(path)
            if path == target:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info

        with patch.object(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400, create=True):
            with patch.object(Path, "lstat", reparse):
                with self.assertRaisesRegex(ProcessError, "link or reparse"):
                    self.assign("current", "fresh")

    def test_concurrent_review_starts_cannot_claim_one_context_twice(self) -> None:
        self.prepare("other")
        processes = []
        try:
            for change in ("current", "other"):
                command = [
                    sys.executable, str(PROCESS_ROOT / "processctl.py"), "change", "review", "start",
                    "--project-root", str(self.root), "--process-root", str(PROCESS_ROOT),
                    "--change-id", change, "--actor", f"reviewer-{change}",
                    "--context", "one-native-context", "--json",
                ]
                processes.append(subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            outputs = [process.communicate(timeout=45) for process in processes]
            self.assertEqual([0, 2], sorted(process.returncode for process in processes), outputs)
            rejected = next(stdout for process, (stdout, _) in zip(processes, outputs) if process.returncode)
            self.assertIn("used by another change", json.loads(rejected)["errors"][0])
            phases = [read_json(self.state_path(change))["phase"] for change in ("current", "other")]
            self.assertEqual(["review-pending", "verified"], sorted(phases))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

    def test_review_gates_exclude_foreign_participant_writes(self) -> None:
        self.prepare("previous")
        self.reopen("previous")
        before = self.state_path("previous").read_bytes()
        original = review_contexts._read_run
        attempts = 0

        def register():
            return begin_implementation(self.root, PROCESS_ROOT, "previous",
                                        actor_id="late-worker", context_id="racing-context", kind="agent")

        def attempt_write(path, parents, process_root):
            nonlocal attempts
            result = original(path, parents, process_root)
            if path == self.state_path("previous"):
                attempts += 1
                with self.assertRaisesRegex(ProcessError, "lock timed out"):
                    register()
            return result

        with patch.object(review_contexts, "_read_run", attempt_write), patch.object(
            review_contexts, "LOCK_TIMEOUT_SECONDS", 0
        ):
            self.assign("current", "racing-context")
            submit_review(self.root, PROCESS_ROOT, "current", self.report("current"))
            finish_change(self.root, PROCESS_ROOT, "current", actor_id="author",
                          context_id="author-current", kind="agent")
        self.assertEqual(3, attempts)
        self.assertEqual(before, self.state_path("previous").read_bytes())
        self.assertEqual("racing-context", register()["implementations"][-1]["actor"]["contextId"])

    def test_verification_preserves_participants_registered_during_profile(self) -> None:
        self.prepare("previous")
        self.reopen("previous")
        original = lifecycle.run_profile

        def register_during_profile(*args):
            report = original(*args)
            begin_implementation(self.root, PROCESS_ROOT, "previous", actor_id="late-worker",
                                 context_id="late-context", kind="agent")
            return report

        with patch.object(lifecycle, "run_profile", register_during_profile), patch.object(
            review_contexts, "LOCK_TIMEOUT_SECONDS", 0
        ):
            state, _ = verify_change(self.root, PROCESS_ROOT, self.fixture.project,
                                     "previous", "development")
        self.assertEqual("late-context", state["implementations"][-1]["actor"]["contextId"])
        self.assertTrue(any(event["actor"]["contextId"] == "late-context" for event in state["history"]))
        with self.assertRaisesRegex(ProcessError, "used by another change"):
            self.assign("current", "late-context")

    def test_overlapping_profiles_preserve_both_results(self) -> None:
        self.reopen("current")
        original = lifecycle.run_profile

        def finish_other_profile(*args):
            report = original(*args)
            with patch.object(lifecycle, "run_profile", original):
                verify_change(self.root, PROCESS_ROOT, self.fixture.project, "current", "review")
            return report

        with patch.object(lifecycle, "run_profile", finish_other_profile), patch.object(
            review_contexts, "LOCK_TIMEOUT_SECONDS", 0
        ):
            state, _ = verify_change(self.root, PROCESS_ROOT, self.fixture.project, "current", "development")
        self.assertEqual("verified", state["phase"])
        self.assertEqual({"development", "review"}, set(state["verification"]))

    def test_verification_cannot_overwrite_a_new_implementation_cycle(self) -> None:
        self.reopen("current")
        original = lifecycle.run_profile
        advanced = None

        def advance_cycle(*args):
            nonlocal advanced
            report = original(*args)
            with patch.object(lifecycle, "run_profile", original):
                for profile in ("development", "review"):
                    verify_change(self.root, PROCESS_ROOT, self.fixture.project, "current", profile)
            self.reopen("current")
            advanced = self.state_path("current").read_bytes()
            return report

        with patch.object(lifecycle, "run_profile", advance_cycle), patch.object(
            review_contexts, "LOCK_TIMEOUT_SECONDS", 0
        ):
            with self.assertRaisesRegex(ProcessError, "implementation cycle changed"):
                verify_change(self.root, PROCESS_ROOT, self.fixture.project, "current", "development")
        self.assertEqual(advanced, self.state_path("current").read_bytes())

    def test_cli_replaces_only_the_recorded_invalid_assignment(self) -> None:
        before = self.historical_reuse()
        result = subprocess.run(
            [sys.executable, str(PROCESS_ROOT / "processctl.py"), "change", "review", "replace-reused",
             "--project-root", str(self.root), "--process-root", str(PROCESS_ROOT),
             "--change-id", "current", "--actor", "new-reviewer", "--context", "new-context", "--json"],
            capture_output=True, timeout=45, check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual("change review replace-reused", output["command"])
        self.assertEqual(before["cycle"], output["cycle"])
        self.assertEqual("new-context", output["assignment"]["reviewer"]["contextId"])


if __name__ == "__main__":
    unittest.main()
