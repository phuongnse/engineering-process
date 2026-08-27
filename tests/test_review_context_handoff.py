import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from engineering_process.contracts import ContractError
from engineering_process.lifecycle import (
    begin_implementation,
    lifecycle_status,
    submit_plan_decision_review,
    verify_change,
)
from tests import test_lifecycle as lifecycle_fixtures
from verification.transfer_review_context_reservation import (
    export_reservation,
    restore_reservation,
)


class ReviewContextHandoffTests(unittest.TestCase):
    def setUp(self):
        self.fixture = lifecycle_fixtures.LifecycleTests(
            "test_authored_plan_decision_evidence_survives_completion_export"
        )

    def _prepare(self, root: Path, inputs: Path):
        self.fixture.initialize_repository(root)
        inputs.mkdir()
        return self.fixture.prepare_authored_plan_decision(root, inputs)

    def test_exact_reservation_handoff_reaches_both_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            project, assignment = self._prepare(source, base / "inputs")
            assignment_path = (
                source
                / ".process"
                / "runs"
                / "change-1"
                / "plan-decision-assignment-1.json"
            )
            handoff = base / "handoff"

            exported = export_reservation(source, assignment_path, handoff)

            restored = base / "restored"
            subprocess.run(
                ["git", "clone", "-q", str(source), str(restored)], check=True
            )
            restored_run = restored / ".process" / "runs" / "change-1"
            restored_run.parent.mkdir(parents=True)
            shutil.copytree(
                source / ".process" / "runs" / "change-1", restored_run
            )
            restored_assignment = restored_run / "plan-decision-assignment-1.json"
            restored_result = restore_reservation(
                restored, restored_assignment, handoff
            )
            review_path = base / "plan-decision-review.json"
            self.fixture.write_plan_decision_review(
                review_path, assignment
            )

            submit_plan_decision_review(
                restored, project, "change-1", review_path
            )
            begin_implementation(
                restored,
                "change-1",
                project=project,
                actor_id="worker",
                context_id="worker-context",
                kind="agent",
            )
            for profile in ("development", "review"):
                verify_change(
                    restored,
                    project,
                    "change-1",
                    profile,
                    actor_id="worker",
                    context_id="worker-context",
                    kind="agent",
                )

            self.assertEqual("passed", exported["status"])
            self.assertEqual("passed", restored_result["status"])
            self.assertEqual(
                "verified", lifecycle_status(restored, "change-1")["phase"]
            )
            with self.assertRaisesRegex(
                ContractError, "registry must be empty before restore"
            ):
                restore_reservation(restored, restored_assignment, handoff)

    def test_missing_extra_and_mismatched_reservations_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            _project, _assignment = self._prepare(source, base / "inputs")
            assignment_path = (
                source
                / ".process"
                / "runs"
                / "change-1"
                / "plan-decision-assignment-1.json"
            )
            handoff = base / "handoff"
            export_reservation(source, assignment_path, handoff)

            missing = base / "missing"
            missing.mkdir()
            extra = base / "extra"
            shutil.copytree(handoff, extra)
            (extra / "extra.json").write_text("{}\n", encoding="utf-8")
            mismatch = base / "mismatch"
            shutil.copytree(handoff, mismatch)
            reservation_path = next(mismatch.iterdir())
            reservation = json.loads(reservation_path.read_text(encoding="utf-8"))
            reservation["actorId"] = "different-reviewer"
            reservation_path.write_bytes(
                (
                    json.dumps(
                        reservation, ensure_ascii=False, indent=2, sort_keys=True
                    )
                    + "\n"
                ).encode("utf-8")
            )

            for label, reservation_root, message in (
                ("missing", missing, "exactly the assigned file"),
                ("extra", extra, "exactly the assigned file"),
                ("mismatch", mismatch, "does not match its plan decision assignment"),
            ):
                with self.subTest(label=label), tempfile.TemporaryDirectory() as target:
                    project_root = Path(target)
                    (project_root / ".process" / "runs").mkdir(parents=True)
                    with self.assertRaisesRegex(ContractError, message):
                        restore_reservation(
                            project_root, assignment_path, reservation_root
                        )


if __name__ == "__main__":
    unittest.main()
