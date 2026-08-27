import copy
import unittest

from verification.validate_release_plan_continuation import (
    ContinuationError,
    require_artifact_absent,
    select_planned_artifact,
    validate_continuation,
)


class ReleasePlanContinuationTests(unittest.TestCase):
    repository = "phuongnse/engineering-process"
    protected_base = "a" * 40

    def workflow(self):
        return {
            "id": 771,
            "name": "Verify unpublished Release candidate",
            "path": ".github/workflows/release-candidate.yml",
        }

    def run_document(self):
        return {
            "id": 991,
            "run_attempt": 2,
            "workflow_id": 771,
            "name": "Verify unpublished Release candidate",
            "path": ".github/workflows/release-candidate.yml",
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "head_sha": self.protected_base,
            "repository": {"full_name": self.repository},
            "head_repository": {"full_name": self.repository},
        }

    def validate(self, run=None, workflow=None):
        return validate_continuation(
            run or self.run_document(),
            workflow or self.workflow(),
            repository=self.repository,
            planned_run_id=991,
            planned_run_attempt=2,
            protected_base=self.protected_base,
        )

    def test_accepts_exact_protected_base_run(self):
        result = self.validate()
        self.assertEqual(self.protected_base, result["workflow"]["sha"])
        self.assertEqual(2, result["run"]["attempt"])

    def test_rejects_same_name_run_from_another_ref(self):
        run = copy.deepcopy(self.run_document())
        run["head_sha"] = "b" * 40
        with self.assertRaisesRegex(ContinuationError, "protected base"):
            self.validate(run=run)

    def test_rejects_same_name_run_from_another_workflow_path(self):
        run = copy.deepcopy(self.run_document())
        run["path"] = ".github/workflows/untrusted.yml"
        with self.assertRaisesRegex(ContinuationError, "workflow path"):
            self.validate(run=run)

    def test_rejects_wrong_attempt_or_workflow_identity(self):
        run = copy.deepcopy(self.run_document())
        run["run_attempt"] = 1
        with self.assertRaisesRegex(ContinuationError, "attempt"):
            self.validate(run=run)
        run = copy.deepcopy(self.run_document())
        run["workflow_id"] = 772
        with self.assertRaisesRegex(ContinuationError, "workflow id"):
            self.validate(run=run)

    def test_rejects_fork_repository_and_nonterminal_run(self):
        run = copy.deepcopy(self.run_document())
        run["head_repository"] = {"full_name": "attacker/fork"}
        with self.assertRaisesRegex(ContinuationError, "repository identity"):
            self.validate(run=run)
        run = copy.deepcopy(self.run_document())
        run["status"] = "in_progress"
        run["conclusion"] = None
        with self.assertRaisesRegex(ContinuationError, "terminal"):
            self.validate(run=run)

    def test_single_use_artifact_selection_and_replay_rejection(self):
        name = f"planned-release-candidate-{'c' * 40}"
        pages = [{"artifacts": [{"id": 123, "name": name, "expired": False}]}]
        self.assertEqual(
            {"id": 123, "name": name},
            select_planned_artifact(pages, expected_name=name),
        )
        require_artifact_absent([{"artifacts": []}], artifact_id=123)
        with self.assertRaisesRegex(ContinuationError, "exactly one"):
            select_planned_artifact([{"artifacts": []}], expected_name=name)

    def test_rejects_duplicate_expired_or_surviving_artifact(self):
        name = f"planned-release-candidate-{'d' * 40}"
        duplicate = {
            "artifacts": [
                {"id": 123, "name": name, "expired": False},
                {"id": 124, "name": name, "expired": False},
            ]
        }
        with self.assertRaisesRegex(ContinuationError, "exactly one"):
            select_planned_artifact([duplicate], expected_name=name)
        expired = {"artifacts": [{"id": 123, "name": name, "expired": True}]}
        with self.assertRaisesRegex(ContinuationError, "exactly one"):
            select_planned_artifact([expired], expected_name=name)
        with self.assertRaisesRegex(ContinuationError, "still exists"):
            require_artifact_absent([duplicate], artifact_id=123)


if __name__ == "__main__":
    unittest.main()
