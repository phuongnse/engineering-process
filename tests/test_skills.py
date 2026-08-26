import tempfile
import unittest
from pathlib import Path

from engineering_process.contracts import ContractError
from engineering_process.skills import skill_digest, validate_skills


PROCESS_ROOT = Path(__file__).resolve().parent.parent


class SkillTests(unittest.TestCase):
    def test_failure_to_invariant_protocol_is_distribution_owned(self):
        skills = PROCESS_ROOT / "process_assets" / "skills"
        execution = (skills / "run-change" / "references" / "execution.md").read_text(
            encoding="utf-8"
        )
        run_change = (skills / "run-change" / "SKILL.md").read_text(encoding="utf-8")
        evolve = (skills / "evolve-process" / "SKILL.md").read_text(encoding="utf-8")
        cross_repo = (skills / "cross-repo-change" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        review = (skills / "review-change" / "SKILL.md").read_text(encoding="utf-8")
        production = (PROCESS_ROOT / "PRODUCTION_STANDARD.md").read_text(encoding="utf-8")

        self.assertIn("## Failure-to-invariant protocol", execution)
        self.assertIn("`project-local`, `shared-process`", execution)
        self.assertIn("A shared-process defect must be fixed in", execution)
        self.assertIn("valid\n   behavior", execution)
        self.assertIn("bounded, idempotent", execution)
        self.assertIn("failure-to-invariant protocol", run_change)
        self.assertIn("keep the consumer candidate blocked", evolve)
        self.assertIn("real\n   affected-consumer reproduction", evolve)
        self.assertIn("mandatory `improvement-required` phase", evolve)
        self.assertIn("In `improvement-pending`", cross_repo)
        self.assertIn("improvement-required", run_change)
        self.assertIn("immutable\nrelease resolution", execution)
        self.assertIn("Treat owner mismatch", review)
        self.assertIn("## Failure to invariant", production)

    def test_repository_skills_are_portable(self):
        self.assertEqual(
            validate_skills(PROCESS_ROOT / "process_assets" / "skills"), []
        )

    def test_controlled_proposals_preserve_versioned_merge_boundaries(self):
        skills = PROCESS_ROOT / "process_assets" / "skills"
        execution = (skills / "run-change" / "references" / "execution.md").read_text(
            encoding="utf-8"
        )
        publish = (skills / "publish-change" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        run_change = (skills / "run-change" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        versioning = (PROCESS_ROOT / "VERSIONING.md").read_text(encoding="utf-8")

        for text in (execution, publish):
            normalized = text.lower().replace("-", " ")
            self.assertIn("controlled automation proposal", normalized)
            self.assertIn("validate-proposal-completion", text)
            self.assertIn("exact head", normalized)
            self.assertIn("automerge", text)
            self.assertIn("process-authority", text)
        self.assertIn("Missing or disabled base policy fails closed", execution)
        for text in (execution, publish):
            self.assertIn("consumerOwnerMergeRequired", text)
            self.assertIn("consumer owner", text.lower())
            self.assertIn("merge is terminal", text.lower())
            self.assertIn("agent-host", text.lower())
            self.assertIn("reviewer host", text.lower())
        self.assertIn("standing auto-merge authority does not apply", publish)
        self.assertIn("may retain standing automation", publish)
        self.assertIn("default agent-host route", publish)
        self.assertIn("explicit exception", publish)
        self.assertIn("default agent-host route", run_change.lower())
        self.assertIn("default agent-host completion-first route", versioning)
        self.assertIn("explicit exception", versioning)

    def test_standing_policy_continues_automation_and_escalates_only_exceptions(self):
        skills = PROCESS_ROOT / "process_assets" / "skills"
        execution = (skills / "run-change" / "references" / "execution.md").read_text(
            encoding="utf-8"
        )
        run_change = (skills / "run-change" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        publish = (skills / "publish-change" / "SKILL.md").read_text(
            encoding="utf-8"
        )

        for text in (execution, run_change, publish):
            self.assertIn("standing", text.lower())
            self.assertIn("exact-head", text.lower())
        for reason in (
            "bounded-recovery-exhausted",
            "capability-unavailable",
            "decision-required",
        ):
            self.assertIn(reason, execution + publish)
        self.assertNotIn("human merge boundary", run_change.lower())
        self.assertNotIn("do not merge on behalf", publish.lower())

    def test_material_recommendations_validate_invariants_before_optimization(self):
        skills = PROCESS_ROOT / "process_assets" / "skills"
        execution = (skills / "run-change" / "references" / "execution.md").read_text(
            encoding="utf-8"
        )
        assess = (skills / "assess-design" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        production = (PROCESS_ROOT / "PRODUCTION_STANDARD.md").read_text(
            encoding="utf-8"
        )

        for text in (execution, assess, production):
            normalized = " ".join(text.lower().split())
            self.assertIn("invariant", normalized)
            self.assertIn("optimization", normalized)
            self.assertIn("invalid", normalized)
            self.assertIn("unproven", normalized)
            self.assertIn("independent adversarial", normalized)
        normalized_execution = " ".join(execution.split())
        self.assertIn(
            "processctl recommendation validate-chain", normalized_execution
        )
        self.assertIn(
            "processctl recommendation review start", normalized_execution
        )
        self.assertIn("project-global context", normalized_execution)
        self.assertIn(
            "grants no lifecycle completion", " ".join(production.split())
        )

    def test_plan_decision_gate_rejects_meta_review_and_reviewer_platforms(self):
        skills = PROCESS_ROOT / "process_assets" / "skills"
        execution = (skills / "run-change" / "references" / "execution.md").read_text(
            encoding="utf-8"
        )
        plan = (skills / "plan-change" / "SKILL.md").read_text(encoding="utf-8")
        implement = (skills / "implement-change" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        combined = " ".join((execution + plan + implement).lower().split())

        for required in (
            "provenance-gated-authored-review",
            "exactly recomputes the complete plan",
            "unreviewed prose is candidate-only",
            "reviewer-of-reviewer",
            "assessment-of-assessment",
            "dynamically generated approval chain",
            "generic workflow engine",
            "hosted reviewer platform",
        ):
            self.assertIn(required, combined)
        self.assertIn("exact generated plans do not receive universal", combined)

    def test_digest_changes_with_instruction_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "sample-skill"
            skill.mkdir()
            path = skill / "SKILL.md"
            path.write_text(
                "---\n"
                "name: sample-skill\n"
                "description: Perform a sample task when requested.\n"
                "---\n\n"
                "# Sample\n\n"
                "Perform the task.\n",
                encoding="utf-8",
            )
            first = skill_digest(root)
            path.write_text(path.read_text(encoding="utf-8") + "\nVerify it.\n")

            self.assertNotEqual(first, skill_digest(root))

    def test_rejects_agent_specific_core_instruction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "sample-skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\n"
                "name: sample-skill\n"
                "description: Perform a sample task when requested.\n"
                "---\n\n"
                "# Sample\n\n"
                "Use spawn_agent to perform the task.\n",
                encoding="utf-8",
            )

            self.assertTrue(
                any("agent-specific" in issue for issue in validate_skills(root))
            )

    def test_digest_refuses_invalid_skills(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "Bad_Name"
            skill.mkdir()
            (skill / "SKILL.md").write_text("not a skill", encoding="utf-8")

            with self.assertRaises(ContractError):
                skill_digest(root)
