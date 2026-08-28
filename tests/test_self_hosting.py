import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
import yaml

from engineering_process import VERSION


PROCESS_ROOT = Path(__file__).resolve().parent.parent


class SelfHostingTests(unittest.TestCase):
    def test_authority_transition_workflow_uses_source_owned_verifier(self):
        workflow = (
            PROCESS_ROOT / ".github" / "workflows" / "authority-transition.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("bootstrap_authorization_gzip_base64", workflow)
        self.assertIn('test "${#AUTHORIZATION_BASE64}" -le 60000', workflow)
        self.assertIn('".transition-controller/$VERIFIER_ENTRYPOINT"', workflow)
        self.assertIn("ref: ${{ env.VERIFIER_COMMIT }}", workflow)
        self.assertIn("ref: ${{ inputs.candidate_head }}", workflow)
        self.assertIn("authority-transition-completion", (PROCESS_ROOT / "examples" / "protected-transition-policy.json").read_text(encoding="utf-8"))
        self.assertIn('gh pr merge "$PR_NUMBER"', workflow)
        self.assertIn("--auto --squash", workflow)
        self.assertIn('--match-head-commit "$HEAD_SHA"', workflow)
        self.assertIn("expected-target-assets.txt", workflow)
        self.assertIn("-le 128000000", workflow)
        self.assertIn("-le 256000000", workflow)
        self.assertNotIn("processctl change finish", workflow)
        self.assertIn("validate_protected_transition_callback.py", workflow)
        self.assertIn("build_transition_validation_service.py", workflow)
        self.assertIn("build_target_repository_proof.py", workflow)
        self.assertIn("target-repository-proof.json", workflow)
        self.assertIn("--paginate --slurp", workflow)
        self.assertIn("filter=all", workflow)
        self.assertIn("--validation-service", workflow)
        for uses in re.findall(r"(?m)^\s*-?\s*uses:\s*([^\s#]+)", workflow):
            if uses.startswith("./"):
                continue
            self.assertRegex(uses, r"@[0-9a-f]{40}$")

        consumption = (
            PROCESS_ROOT
            / ".github"
            / "workflows"
            / "authority-transition-consumption.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("authority-transition bootstrap consume", consumption)
        self.assertIn("github.event.pull_request.merged_at", consumption)
        self.assertIn("resolve_transition_consumption_service.py", consumption)
        self.assertIn("validate_transition_check_exclusivity.py", consumption)
        self.assertIn("--paginate --slurp", consumption)
        self.assertIn("filter=all", consumption)
        self.assertIn("consumption-check-request.json", consumption)
        self.assertIn("consumptionContext", consumption)
        self.assertIn("validationArtifact", (PROCESS_ROOT / "schemas" / "bootstrap-adoption-consumption.schema.json").read_text(encoding="utf-8"))
        workflow_document = yaml.safe_load(workflow)
        self.assertEqual(
            {"contents": "read", "pull-requests": "read"},
            workflow_document["permissions"],
        )
        steps = workflow_document["jobs"]["validate-and-merge"]["steps"]
        app_token = next(step for step in steps if step.get("id") == "app-token")
        self.assertEqual("write", app_token["with"]["permission-checks"])
        self.assertEqual("write", app_token["with"]["permission-pull-requests"])
        base_step = next(step for step in steps if step.get("name") == "Require current base and exact PR identity")
        self.assertEqual("${{ github.workflow_sha }}", base_step["env"]["WORKFLOW_SHA"])
        consumption_document = yaml.safe_load(consumption)
        self.assertEqual(
            {"actions": "read", "checks": "read", "contents": "read"},
            consumption_document["permissions"],
        )
        consumption_steps = consumption_document["jobs"]["record"]["steps"]
        consumption_token = next(
            step for step in consumption_steps if step.get("id") == "app-token"
        )
        self.assertEqual("write", consumption_token["with"]["permission-checks"])
        self.assertNotIn("permission-pull-requests", consumption_token["with"])

    def test_public_install_action_uses_immutable_checkout_source_and_safe_inputs(self):
        action = (PROCESS_ROOT / "action.yml").read_text(encoding="utf-8")
        ci = (
            PROCESS_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("using: composite", action)
        self.assertIn("shell: python", action)
        self.assertIn("${{ github.action_path }}", action)
        self.assertIn('action_root / "verification" / "install_process_runtime.py"', action)
        self.assertIn('sys.argv = arguments', action)
        self.assertNotIn("curl ", action)
        self.assertNotIn("wget ", action)
        self.assertNotIn("shell: bash", action)
        self.assertNotIn("shell: pwsh", action)

        self.assertIn("Smoke test shared install action", ci)
        self.assertIn("process-action-smoke/Scripts/python.exe", ci)
        self.assertIn("process-action-smoke/bin/python", ci)
        self.assertIn("Verify shared install action authority", ci)
        self.assertEqual(3, ci.count("--controller-requirement engineering_process/requirements-"))
        windows_job = ci.index("Verify real Windows Job Object boundary")
        install_wheel = ci.index("Install built wheel")
        windows_job_block = ci[windows_job:install_wheel]
        self.assertIn("if: runner.os == 'Windows'", windows_job_block)
        self.assertIn(
            "run: python -m unittest tests.test_windows_job",
            windows_job_block,
        )
        self.assertLess(ci.index("Build distributions"), windows_job)
        self.assertLess(windows_job, install_wheel)
        create = ci.index("Create exact public N-1 release qualification environment")
        install = ci.index("Install exact public N-1 release qualification authority")
        dependencies = ci.index("Install release qualification dependencies")
        self.assertLess(create, install)
        self.assertLess(install, dependencies)
        install_block = ci[install:dependencies]
        self.assertIn("uses: ./", install_block)
        self.assertIn("requirements-lock: requirements/process.txt", install_block)
        self.assertNotIn("python verification/install_process_runtime.py", ci)
        installer = (
            PROCESS_ROOT / "verification" / "install_process_runtime.py"
        ).read_text(encoding="utf-8")
        self.assertIn("run_bounded_process", installer)
        self.assertNotIn("def _windows_wrapped_command", installer)
        self.assertNotIn('"--status-handle"', installer)

    def test_release_candidate_is_reviewed_before_pr_publication(self):
        candidate = (
            PROCESS_ROOT / ".github" / "workflows" / "release-candidate.yml"
        ).read_text(encoding="utf-8")
        plan_approval = (
            PROCESS_ROOT
            / ".github"
            / "workflows"
            / "release-plan-approval.yml"
        ).read_text(encoding="utf-8")
        approval = (
            PROCESS_ROOT / ".github" / "workflows" / "release-approval.yml"
        ).read_text(encoding="utf-8")
        generator = (
            PROCESS_ROOT / ".github" / "workflows" / "release-pr.yml"
        ).read_text(encoding="utf-8")
        plan_dispatch = (
            PROCESS_ROOT
            / "verification"
            / "render_release_plan_review_dispatch.py"
        ).read_text(encoding="utf-8")
        ci = (
            PROCESS_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        readme = (PROCESS_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", candidate)
        self.assertNotIn("pull_request:", candidate)
        self.assertIn("Verify exact unpublished checkpoint", candidate)
        self.assertIn("Preserve the exact verified checkpoint and lifecycle", candidate)
        self.assertNotIn("processctl change review start", candidate)
        self.assertNotIn("git push", candidate)
        self.assertNotIn("gh pr create", candidate)
        self.assertIn("engineering-process-review-required", candidate)
        self.assertIn("engineering-process-plan-review-required", plan_dispatch)
        self.assertIn("--planned-run-id", candidate)
        self.assertIn("--planned-run-attempt", candidate)
        self.assertIn("planned-release-candidate", candidate)
        self.assertIn("render_release_plan_review_dispatch.py", candidate)
        self.assertIn(
            '"$RUNNER_TEMP/render-release-plan-review-dispatch.py"', candidate
        )
        self.assertLess(
            candidate.index("Preserve the base-controlled plan handoff adapter"),
            candidate.index("Restore the unpublished source bundle"),
        )
        self.assertIn("transfer_review_context_reservation.py export", candidate)
        self.assertIn("processctl change decision start", candidate)
        self.assertIn("steps.identity.outputs.plan_kind == 'authored'", candidate)
        self.assertIn(
            "steps.identity.outputs.plan_kind == 'process-generated'", candidate
        )
        self.assertIn("consumer-selected reviewer host", candidate)
        self.assertIn('publicationWorkflow: $publicationWorkflow', candidate)
        self.assertIn('completionEvidenceEncoding: $completionEvidenceEncoding', candidate)
        self.assertNotIn("processctl change review start", approval)
        self.assertNotIn("--review-report", approval)
        self.assertNotIn("processctl change finish", approval)
        self.assertIn("completion_evidence_gzip_base64", approval)
        self.assertIn(
            '"$RUNNER_TEMP/process-authority/bin/python" processctl.py', approval
        )
        self.assertIn("publication release-pr-body", approval)
        self.assertIn("verification/decode_completion_evidence.py", approval)
        self.assertIn("verification/validate_release_completion_identity.py", approval)
        self.assertIn(
            "cp verification/validate_release_completion_identity.py", approval
        )
        self.assertIn(
            '"$RUNNER_TEMP/validate-release-completion-identity.py"', approval
        )
        self.assertIn("processctl evidence validate", approval)
        self.assertIn("--release-change .release/change.json", approval)
        self.assertIn('--evidence "$RUNNER_TEMP/completion-evidence.json"', approval)
        self.assertIn("--process-lock .process/process.lock", approval)
        self.assertIn('--expected-checkpoint "$HEAD_SHA"', approval)
        self.assertNotIn(
            'jq -r .comparisonBase "$RUNNER_TEMP/completion-summary.json")" = "$BASE_SHA"',
            approval,
        )
        self.assertIn(
            'test "$(jq -r .baseSha "$RUNNER_TEMP/verified-candidate/candidate.json")" = "${{ inputs.comparison_base }}"',
            approval,
        )
        current_base_gate = (
            'test "$(git ls-remote origin refs/heads/main | cut -f1)" = "$BASE_SHA"'
        )
        self.assertEqual(2, approval.count(current_base_gate))
        preserve_adapter = approval.index(
            "Validate current protected candidate base and preserve base-owned adapters"
        )
        restore_candidate = approval.index("Restore the exact unpublished candidate")
        validate_completion = approval.index("Decode and validate host completion evidence")
        self.assertLess(preserve_adapter, restore_candidate)
        self.assertLess(restore_candidate, validate_completion)
        self.assertIn("publication validate-evidence-source", approval)
        self.assertLess(
            approval.index("publication validate-evidence-source"),
            approval.index("git push origin"),
        )
        self.assertLess(approval.index("git push origin"), approval.index("gh pr create"))
        self.assertNotIn("gh pr ready", approval)
        self.assertIn("reconcile_completed_release.py", approval)
        self.assertIn("--state all", approval)
        self.assertIn("--limit 20", approval)
        self.assertIn('if test "$action" = merged; then', approval)
        self.assertIn("if test \"$action\" = existing", approval)
        self.assertNotIn('if test "$action" = existing; then\n            exit 0', approval)
        self.assertIn("enable protected auto-merge", approval)
        self.assertNotIn(
            "processctl.py contract validate --kind automation-policy", approval
        )
        self.assertIn('git show "$BASE_SHA:.process/automation.json"', approval)
        self.assertIn(
            'git show "$BASE_SHA:verification/validate_protected_automation_policy.py"',
            approval,
        )
        self.assertIn(
            'cmp "$RUNNER_TEMP/protected-base-automation.json" .process/automation.json',
            approval,
        )
        self.assertIn("validate-protected-automation-policy.py", approval)
        self.assertIn("protected-automation-summary.json", approval)
        self.assertIn(".pullRequestNumber", approval)
        self.assertNotIn("'.[0].number'", approval)
        self.assertIn('gh pr merge "$pr_number"', approval)
        self.assertIn('--auto "--$merge_method" --match-head-commit "$HEAD_SHA"', approval)
        self.assertIn("gh auth setup-git --hostname github.com", approval)
        create_token = approval.index("Create short-lived source publication token")
        configure_credentials = approval.index(
            "gh auth setup-git --hostname github.com"
        )
        source_push = approval.index('git push origin "$HEAD_SHA:refs/heads/$RELEASE_BRANCH"')
        self.assertLess(create_token, configure_credentials)
        self.assertLess(configure_credentials, source_push)
        self.assertNotIn("x-access-token", approval)
        self.assertNotIn("http.extraheader", approval)
        self.assertNotIn("https://$GH_TOKEN", approval)
        self.assertLess(approval.index("gh pr create"), approval.index("gh pr merge"))
        merged_terminal = approval.index('if test "$action" = merged; then')
        post_reconciliation_base_gate = approval.index(
            current_base_gate, merged_terminal
        )
        self.assertLess(merged_terminal, post_reconciliation_base_gate)
        self.assertIn("Project-specific: Completion evidence", approval)
        self.assertIn("release-changes/*.json", generator)
        self.assertIn('".process/process.lock"', generator)
        self.assertIn('"requirements/process.in"', generator)
        self.assertIn('"requirements/process.txt"', generator)
        self.assertIn("verification/classify_release_preparation.py", generator)
        self.assertIn("verification/validate_release_candidate_commit.py", generator)
        self.assertIn('".github/workflows/release-pr.yml"', generator)
        self.assertNotIn("gh pr create", generator)
        self.assertNotIn("gh pr ready", generator)
        self.assertNotIn("git push", generator)
        self.assertIn("source.bundle", generator)
        self.assertIn("unpublished-release-candidate", generator)
        self.assertIn("Detect pending release changes", generator)
        self.assertIn("No pending release changes", generator)
        self.assertIn("git add --all", generator)
        self.assertNotIn("git add --all --", generator)
        self.assertIn("release-candidate-commit.json", generator)
        self.assertLess(
            generator.index("git commit"),
            generator.index("validate_release_candidate_commit.py", generator.index("git commit")),
        )
        self.assertLess(
            generator.index("validate_release_candidate_commit.py", generator.index("git commit")),
            generator.index("git bundle create"),
        )
        self.assertIn('gh workflow run release-candidate.yml', generator)
        self.assertIn('".github/workflows/release-plan-approval.yml"', generator)
        self.assertIn("policy-verification:", ci)
        self.assertIn(
            "phuongnse/renovate-ops/.github/workflows/policy-verification.yml@"
            "2152dab51edd6c84163a71b48f50e6ad042eb331",
            ci,
        )
        self.assertNotIn("independent-review.yml", ci)
        self.assertNotIn("release-authorization:", ci)
        self.assertIn("python templates/adopt-process.py", ci)
        self.assertIn("--check", ci)
        self.assertNotIn("processctl adoption check", ci)
        self.assertNotIn("python .process/adopt-process.py", ci)
        self.assertNotIn("host-review.json", approval)
        self.assertIn("plan_decision_review_gzip_base64", plan_approval)
        self.assertIn("planned_run_attempt", plan_approval)
        self.assertIn("planned-release-candidate", plan_approval)
        self.assertIn("transfer-review-context-reservation.py", plan_approval)
        self.assertIn("review-contexts", plan_approval)
        self.assertIn("processctl change decision submit", plan_approval)
        self.assertIn("processctl change implement", plan_approval)
        self.assertIn("--profile development", plan_approval)
        self.assertIn("--profile review", plan_approval)
        self.assertIn("engineering-process-review-required", plan_approval)
        self.assertIn("actions/workflows/release-candidate.yml", plan_approval)
        self.assertIn("validate_release_plan_continuation.py", plan_approval)
        self.assertIn('--protected-base "$BASE_SHA"', plan_approval)
        self.assertIn('--planned-run-attempt "$PLANNED_RUN_ATTEMPT"', plan_approval)
        self.assertNotIn("gh run view", plan_approval)
        self.assertIn("actions/artifacts/$ARTIFACT_ID", plan_approval)
        self.assertIn("--method DELETE", plan_approval)
        self.assertIn("plan-continuation-terminal.json", plan_approval)
        self.assertIn("${{ needs.continue.result }}", plan_approval)
        self.assertIn(
            "always() && needs.continue.outputs.planned_artifact_id != ''",
            plan_approval,
        )
        self.assertIn("remaining-planned-artifact-pages.json", plan_approval)
        self.assertNotIn("processctl change finish", plan_approval)
        self.assertNotIn("git push", plan_approval)
        self.assertNotIn("gh pr create", plan_approval)
        self.assertNotIn("gh pr merge", plan_approval)
        submit_plan_review = plan_approval.index(
            "processctl change decision submit"
        )
        restore_context = plan_approval.index(
            "Restore the exact assignment-bound context reservation"
        )
        implement_plan = plan_approval.index("processctl change implement")
        authorize_plan = plan_approval.index(".planDecision.authorized")
        upload_verified = plan_approval.index(
            "Upload the host-neutral source-review handoff"
        )
        consume_planned = plan_approval.index(
            "Consume and reconcile the single-use planned artifact"
        )
        preserve_terminal = plan_approval.index(
            "Preserve bounded plan-continuation terminal diagnostics"
        )
        dispatch_source_review = plan_approval.index(
            "Dispatch the immutable checkpoint to the consumer-selected source reviewer"
        )
        self.assertLess(restore_context, submit_plan_review)
        self.assertLess(submit_plan_review, implement_plan)
        self.assertLess(implement_plan, authorize_plan)
        self.assertLess(implement_plan, upload_verified)
        self.assertLess(upload_verified, dispatch_source_review)
        self.assertLess(dispatch_source_review, preserve_terminal)
        self.assertLess(preserve_terminal, consume_planned)
        plan_document = yaml.safe_load(plan_approval)
        continuation_job = plan_document["jobs"]["continue"]
        cleanup_job = plan_document["jobs"]["consume-planned-artifact"]
        self.assertEqual(
            "${{ steps.planned.outputs.artifact_id }}",
            continuation_job["outputs"]["planned_artifact_id"],
        )
        self.assertEqual("continue", cleanup_job["needs"])
        self.assertIn("always()", cleanup_job["if"])
        self.assertEqual(5, cleanup_job["timeout-minutes"])
        self.assertEqual({"actions": "write"}, cleanup_job["permissions"])
        primary_step_names = {
            step.get("name") for step in continuation_job["steps"]
        }
        self.assertNotIn(
            "Consume and reconcile the single-use planned artifact",
            primary_step_names,
        )
        cleanup_steps = {
            step.get("name"): step for step in cleanup_job["steps"]
        }
        self.assertIn(
            "Preserve bounded plan-continuation terminal diagnostics", cleanup_steps
        )
        self.assertEqual(
            "${{ always() }}",
            cleanup_steps[
                "Consume and reconcile the single-use planned artifact"
            ]["if"],
        )
        for workflow in (candidate, plan_approval):
            for uses in re.findall(r"(?m)^\s*-?\s*uses:\s*([^\s#]+)", workflow):
                if uses.startswith("./"):
                    continue
                self.assertRegex(uses, r"@[0-9a-f]{40}$")
        self.assertIn("enables exact-head protected auto-merge", readme)
        self.assertIn("No workflow bypasses branch protection", readme)
        self.assertIn("candidate source base", readme)
        self.assertIn("lifecycle comparison base", readme)
        self.assertNotIn("No workflow invokes merge", readme)

    def test_renovate_consumer_intent_keeps_authority_adoption_disabled(self):
        renovate = json.loads(
            (PROCESS_ROOT / ".github" / "renovate.json").read_text(encoding="utf-8")
        )

        self.assertTrue(renovate["enabled"])
        self.assertFalse(renovate["automerge"])
        self.assertFalse(renovate["dependencyDashboard"])
        self.assertNotIn("draftPR", renovate)
        self.assertNotIn("prCreation", renovate)
        self.assertNotIn("prBodyTemplate", renovate)
        self.assertEqual("automation/renovate/", renovate["branchPrefix"])
        self.assertEqual("==7.6.1", renovate["constraints"]["pipTools"])
        authority_rule = next(
            rule
            for rule in renovate["packageRules"]
            if rule.get("matchPackageNames") == ["engineering-process"]
        )
        self.assertFalse(authority_rule["enabled"])
        self.assertFalse(authority_rule["automerge"])
        self.assertEqual(["at any time"], authority_rule["schedule"])
        self.assertEqual(100, authority_rule["prPriority"])
        self.assertEqual(
            ["requirements/process.in", "requirements/process.txt"],
            authority_rule["matchFileNames"],
        )
        self.assertEqual(
            ["/^requirements\\/process\\.txt$/"],
            renovate["pip-compile"]["managerFilePatterns"],
        )
        self.assertFalse(renovate["pip_requirements"]["enabled"])
        self.assertNotIn("postUpgradeTasks", renovate)
        ci = (PROCESS_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("automation/process/engineering-process", ci)
        self.assertNotIn("automation/renovate/engineering-process", ci)

    def test_release_assets_are_prepared_before_immutable_publication(self):
        prepare = (
            PROCESS_ROOT / ".github" / "workflows" / "prepare-release.yml"
        ).read_text(encoding="utf-8")
        publish = (
            PROCESS_ROOT / ".github" / "workflows" / "publish.yml"
        ).read_text(encoding="utf-8")
        release = (
            PROCESS_ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("workflow_call:", prepare)
        self.assertNotIn("workflow_dispatch:", prepare)
        self.assertIn("isDraft", prepare)
        self.assertIn("gh release upload", prepare)
        self.assertIn("expected-release-assets.txt", prepare)
        self.assertIn("pull_request_target:", release)
        self.assertIn('".github/workflows/release.yml"', release)
        self.assertIn('".github/workflows/prepare-release.yml"', release)
        self.assertIn('".github/workflows/publish.yml"', release)
        for controller in (
            "check_pypi_publication.py",
            "restore_release_evidence.py",
            "select_release_evidence.py",
            "validate_publish_event.py",
            "verify_distribution.py",
            "verify_installed_distribution.py",
        ):
            self.assertIn(f'"verification/{controller}"', release)
        self.assertIn("automation/release/next", release)
        self.assertIn('refs/pull/$PR_NUMBER/head', release)
        self.assertIn("steps.release-pr.outputs.reviewed_sha", release)
        self.assertIn("steps.release-pr.outputs.release_sha", release)
        self.assertIn("actions/create-github-app-token@", release)
        self.assertIn("permission-workflows: write", release)
        self.assertIn("token: ${{ steps.app-token.outputs.token }}", release)
        self.assertIn("publication authorize-release", release)
        evidence_start = release.index(
            "- name: Restore exact-head pre-publication completion evidence"
        )
        evidence_end = release.index(
            "- name: Prove reviewed tree, merge commit, release contract, and prior tag",
            evidence_start,
        )
        evidence_recovery = release[evidence_start:evidence_end]
        self.assertIn(
            '"$GITHUB_WORKSPACE/.release-controller/verification/restore_release_evidence.py"',
            evidence_recovery,
        )
        self.assertIn('--reviewed-sha "$REVIEWED_SHA"', evidence_recovery)
        self.assertIn('--evidence-asset "$EVIDENCE_ASSET"', evidence_recovery)
        self.assertIn('--output "$RUNNER_TEMP/release-completion"', evidence_recovery)
        self.assertNotIn("python verification/restore_release_evidence.py", evidence_recovery)
        self.assertNotIn("gh run download", evidence_recovery)
        self.assertNotIn("gh release download", evidence_recovery)
        self.assertNotIn("gh release create", evidence_recovery)
        self.assertIn("gh release edit", release)
        for workflow in (release, prepare, publish):
            self.assertIn(".release-controller", workflow)
            self.assertIn("github.workflow_sha", workflow)
        controller_runtime_lock = (
            "$GITHUB_WORKSPACE/.release-controller/engineering_process/"
            "requirements-runtime.txt"
        )
        self.assertEqual(1, prepare.count(controller_runtime_lock))
        self.assertLess(
            prepare.index(controller_runtime_lock),
            prepare.index(".release-controller/processctl.py"),
        )
        publish_environments = publish.split(
            "- name: Install exact public authority and release dependencies"
        )[1:]
        self.assertEqual(2, len(publish_environments))
        for environment in publish_environments:
            self.assertEqual(1, environment.count(controller_runtime_lock))
            self.assertLess(
                environment.index(controller_runtime_lock),
                environment.index(".release-controller/processctl.py"),
            )
        self.assertIn('--project-root "$GITHUB_WORKSPACE"', release)
        self.assertIn("repository_dispatch:", publish)
        self.assertIn("types: [engineering-process-release-ready]", publish)
        self.assertNotIn("release:\n    types: [published]", publish)
        self.assertNotIn("workflow_call:", publish)
        self.assertIn("engineering-process-release-ready", release)
        self.assertIn('"repos/$GITHUB_REPOSITORY/dispatches"', release)
        self.assertIn("needs.authorize.outputs.publish_required == 'false'", release)
        self.assertNotIn("gh release upload", publish)
        self.assertGreaterEqual(publish.count("gh release verify"), 2)
        self.assertGreaterEqual(publish.count("gh release verify-asset"), 2)
        self.assertGreaterEqual(publish.count("attestations: read"), 2)
        self.assertGreaterEqual(publish.count("expected-release-assets.txt"), 4)
        self.assertGreaterEqual(publish.count("actual-release-assets.txt"), 4)
        self.assertGreaterEqual(
            publish.count("gh release view \"$RELEASE_TAG\" --json assets"),
            2,
        )
        self.assertIn(
            "Revalidate immutable release immediately before publication", publish
        )
        self.assertIn("check_pypi_publication.py", publish)
        self.assertIn("validate_publish_event.py", publish)
        self.assertEqual(2, publish.count("download_asset()"))
        self.assertEqual(2, publish.count("for attempt in 1 2 3 4"))
        self.assertEqual(2, publish.count('rm -f "$directory/$pattern"'))
        self.assertIn("--require-published", publish)
        self.assertNotIn("skip-existing", publish)
        self.assertIn("engineering-process-published", publish)
        self.assertIn("repos/phuongnse/renovate-ops/dispatches", publish)
        self.assertIn("repositories: renovate-ops", publish)

    def test_prepare_release_leaves_distribution_output_creation_to_verifier(self):
        prepare = (
            PROCESS_ROOT / ".github" / "workflows" / "prepare-release.yml"
        ).read_text(encoding="utf-8")
        publish = (
            PROCESS_ROOT / ".github" / "workflows" / "publish.yml"
        ).read_text(encoding="utf-8")

        self.assertIn('mkdir -p "$RUNNER_TEMP/draft-assets"', prepare)
        self.assertNotIn(
            'mkdir -p "$RUNNER_TEMP/draft-assets/distributions"', prepare
        )
        self.assertIn(
            '--output "$RUNNER_TEMP/draft-assets/distributions"', prepare
        )
        self.assertIn(
            "python .release-controller/verification/verify_distribution.py",
            prepare,
        )
        self.assertIn('--project-root "$GITHUB_WORKSPACE"', prepare)
        for workflow in (prepare, publish):
            self.assertIn(
                ".release-controller/verification/verify_installed_distribution.py",
                workflow,
            )
            self.assertIn('--source-root "$GITHUB_WORKSPACE"', workflow)
            self.assertNotIn("python -m unittest discover", workflow)

    def test_producer_environment_binds_the_exact_build_backend(self):
        project = json.loads(
            (PROCESS_ROOT / ".process" / "project.json").read_text(encoding="utf-8")
        )
        development = next(
            check
            for check in project["profiles"]["development"]
            if check["id"] == "unit-and-contract-tests"
        )
        requirement = next(
            item
            for item in project["environment"]["requirements"]
            if item["id"] == "development-runtime"
        )

        self.assertIn(
            "version('setuptools') == '84.0.0'",
            requirement["probe"]["run"][2],
        )
        self.assertIn(
            "engineering_process/requirements-build.txt",
            requirement["remediation"],
        )
        self.assertEqual(
            ["python", "verification/run_test_suite.py"], development["run"]
        )
        self.assertIn(
            "python verification/run_test_suite.py",
            (PROCESS_ROOT / "AGENTS.md").read_text(encoding="utf-8"),
        )

    def test_ci_binds_and_uploads_bounded_matrix_evidence(self):
        workflow = (
            PROCESS_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "ref: ${{ inputs.remote_source_ref || github.event.pull_request.head.sha || github.sha }}",
            workflow,
        )
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("remote_request_sha256:", workflow)
        self.assertIn("Validate exact remote verification dispatch", workflow)
        self.assertIn(
            'git show "$REMOTE_WORKFLOW_SHA:verification/validate_remote_verification_dispatch.py"',
            workflow,
        )
        self.assertIn(
            "CI_CHECKPOINT: ${{ inputs.remote_checkpoint || github.event.pull_request.head.sha || github.sha }}",
            workflow,
        )
        self.assertIn(
            "if: github.event_name == 'pull_request'",
            workflow,
        )
        self.assertIn("PR_BODY: ${{ github.event.pull_request.body }}", workflow)
        self.assertNotIn("PR_BODY_PATH:", workflow)
        self.assertIn("verification/generate_ci_evidence.py", workflow)
        self.assertIn(
            "python -m pip install -r engineering_process/requirements-runtime.txt "
            "-r engineering_process/requirements-dev.txt "
            "-r engineering_process/requirements-build.txt",
            workflow,
        )
        self.assertIn(
            'python verification/verify_distribution.py --output "$RUNNER_TEMP/dist"',
            workflow,
        )
        self.assertIn("timeout-minutes: 20", workflow)
        self.assertIn(
            'python -m venv "$RUNNER_TEMP/release-qualification-authority"',
            workflow,
        )
        self.assertIn("uses: ./", workflow)
        self.assertIn("requirements-lock: requirements/process.txt", workflow)
        self.assertIn(
            "python-executable: ${{ runner.temp }}/release-qualification-authority/bin/python",
            workflow,
        )
        self.assertIn("verification/qualify_release_lifecycle.py", workflow)
        self.assertIn(
            "verification/verify_public_n1_review_context_handoff.py", workflow
        )
        self.assertLess(
            workflow.index(
                "Qualify generated release candidate to the reviewer handoff under N-1"
            ),
            workflow.index(
                "Prove reviewed plan continuation under exact public N-1"
            ),
        )
        self.assertIn(
            '--processctl "$RUNNER_TEMP/release-qualification-authority/bin/processctl"',
            workflow,
        )
        self.assertNotIn("release-authorization:", workflow)
        self.assertIn("github.head_ref != 'automation/release/next'", workflow)
        self.assertNotIn('".[dev]"', workflow)
        self.assertIn('--expected-checkpoint "$CI_CHECKPOINT"', workflow)
        self.assertIn('--comparison-base "$CI_COMPARISON_BASE"', workflow)
        self.assertIn('--workflow-sha "$CI_WORKFLOW_SHA"', workflow)
        self.assertIn("CI_WORKFLOW_SHA: ${{ github.workflow_sha }}", workflow)
        self.assertIn(
            "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
            workflow,
        )
        self.assertIn("if-no-files-found: error", workflow)
        self.assertIn("retention-days: 90", workflow)

    def test_distribution_verifier_resolves_the_checkout_before_installed_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            shadow_package = Path(directory) / "engineering_process"
            shadow_package.mkdir()
            (shadow_package / "__init__.py").write_text("", encoding="utf-8")
            environment = os.environ.copy()
            environment["PYTHONPATH"] = directory

            result = subprocess.run(
                [
                    sys.executable,
                    str(PROCESS_ROOT / "verification" / "verify_distribution.py"),
                    "--help",
                ],
                cwd=PROCESS_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("usage: verify_distribution.py", result.stdout)
        self.assertIn("--project-root PROJECT_ROOT", result.stdout)

    def test_installed_distribution_verifier_rejects_source_imports(self):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROCESS_ROOT)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    str(
                        PROCESS_ROOT
                        / "verification"
                        / "verify_installed_distribution.py"
                    ),
                    "--source-root",
                    str(PROCESS_ROOT),
                ],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn(
            "installed engineering_process resolves inside source checkout",
            result.stderr,
        )

    def test_managed_and_distribution_skill_trees_are_separate(self):
        managed = PROCESS_ROOT / ".agents" / "skills"
        sources = PROCESS_ROOT / "process_assets" / "skills"
        source_names = {
            path.parent.name for path in sources.glob("*/SKILL.md")
        }
        managed_names = {
            path.parent.name for path in managed.glob("*/SKILL.md")
        }

        self.assertEqual(source_names, managed_names)
        self.assertTrue(source_names)
        self.assertFalse(list(sources.rglob(".engineering-process.json")))
        for name in sorted(managed_names):
            marker = managed / name / ".engineering-process.json"
            self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["skill"], name)

    def test_public_seed_requirement_matches_process_lock(self):
        lock = json.loads(
            (PROCESS_ROOT / ".process" / "process.lock").read_text(encoding="utf-8")
        )
        requirements = (PROCESS_ROOT / "requirements" / "process.txt").read_text(
            encoding="utf-8"
        )
        match = re.search(
            r"(?m)^engineering-process==(?P<version>[^ ]+) \\\n",
            requirements,
        )

        self.assertIsNotNone(match)
        self.assertEqual(lock["process"]["version"], match.group("version"))
        source = (PROCESS_ROOT / "requirements" / "process.in").read_text(
            encoding="utf-8"
        )
        source_match = re.search(
            r"(?m)^engineering-process==(?P<version>\S+)$", source
        )
        self.assertIsNotNone(source_match)
        self.assertEqual(lock["process"]["version"], source_match.group("version"))

    def test_process_requirements_lock_covers_supported_binary_runtimes(self):
        requirements_path = PROCESS_ROOT / "requirements" / "process.txt"
        requirements = requirements_path.read_text(encoding="utf-8")

        self.assertLess(requirements_path.stat().st_size, 1_000_000)
        self.assertGreaterEqual(requirements.count("--hash=sha256:"), 100)
        for digest in (
            "09f3e5287f94f17b709dc9a9e70865855feee835c861613be144218ce4ca82cc",
            "7322ec6cc9fba9d49ab888bb82d67ac5625627aa168f0165139b17018df3fb8a",
            "8d3469c91dd92ee41b7c95280edbd975ef1ba9195086686623a1c6e8935ce965",
            "a81758ed242b861b72e778ba34d41366441a2e10b16b472784c88da2dea7e2dd",
            "ac777001cdfc28b72477d93c8564bb7583081ea8fb45cdca3d568e0a4f87183c",
            "d721e53758b2cca74990185eb0671dd466d7a388a1a45d0c6f4c13cef41a68ac",
        ):
            self.assertIn(f"--hash=sha256:{digest}", requirements)

    def test_adoption_runner_sources_remain_managed(self):
        managed = PROCESS_ROOT / ".process" / "adopt-process.py"
        template = PROCESS_ROOT / "templates" / "adopt-process.py"
        managed_windows_helper = (
            PROCESS_ROOT / ".process" / "adopt-process-windows-job.py"
        )
        template_windows_helper = (
            PROCESS_ROOT / "templates" / "adopt-process-windows-job.py"
        )
        windows_helper = PROCESS_ROOT / "engineering_process" / "_windows_job.py"

        marker = "# Managed by engineering-process; do not edit.\n"
        self.assertTrue(template.read_text(encoding="utf-8").startswith(marker))
        self.assertTrue(managed.read_text(encoding="utf-8").startswith(marker))
        self.assertEqual(managed.read_bytes(), template.read_bytes())
        self.assertIn('"--status-handle"', template.read_text(encoding="utf-8"))
        self.assertTrue(
            managed_windows_helper.read_text(encoding="utf-8").startswith(marker)
        )
        self.assertIn(
            "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE",
            managed_windows_helper.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            windows_helper.read_bytes(), template_windows_helper.read_bytes()
        )

    def test_distribution_never_packages_managed_skill_copies(self):
        pyproject = (PROCESS_ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('"process_assets/skills/', pyproject)
        self.assertNotIn('".agents/skills/', pyproject)
        self.assertIn('"VERSIONING.md"', pyproject)
        self.assertIn('"PROCESS_IMPROVEMENT.md"', pyproject)
        self.assertIn('"improvement-catalog.json"', pyproject)
        self.assertIn('"templates/adopt-process.py"', pyproject)
        self.assertIn('"templates/adopt-process-windows-job.py"', pyproject)
        self.assertIn(
            '"schemas/adoption-migration.schema.json"', pyproject
        )
        self.assertIn(
            '"schemas/supplemental-verification.schema.json"', pyproject
        )
        self.assertIn(
            '"schemas/improvement-signal.schema.json"', pyproject
        )
        self.assertTrue(
            (PROCESS_ROOT / "engineering_process" / "_windows_job.py")
            .read_text(encoding="utf-8")
            .startswith("# Managed by engineering-process; do not edit.\n")
        )

    def test_version_surfaces_match_the_current_release_contract(self):
        pyproject = tomllib.loads(
            (PROCESS_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        release = json.loads(
            (PROCESS_ROOT / "release.json").read_text(encoding="utf-8")
        )

        self.assertEqual(VERSION, pyproject["project"]["version"])
        self.assertEqual(VERSION, release["version"])

    def test_publish_fails_closed_at_controller_and_hash_boundaries(self):
        workflow = (
            PROCESS_ROOT / ".github" / "workflows" / "publish.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("github.workflow_sha", workflow)
        self.assertIn(".release-controller/processctl.py", workflow)
        self.assertIn("process-authority/bin/python", workflow)
        self.assertGreaterEqual(
            workflow.count('git fetch --no-tags origin "$reviewed_sha"'), 2
        )
        self.assertGreaterEqual(
            workflow.count('"${controller[@]}" evidence validate'), 2
        )
        self.assertIn("evidence validate-bootstrap", workflow)
        self.assertIn("evidence_args+=(--authorization", workflow)
        self.assertNotIn("authority_version=", workflow)
        self.assertIn("pip install --require-hashes", workflow)
        self.assertIn("engineering_process/requirements-release.txt", workflow)
        self.assertIn("--no-deps", workflow)


if __name__ == "__main__":
    unittest.main()
