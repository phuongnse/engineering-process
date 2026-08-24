import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from engineering_process import VERSION


PROCESS_ROOT = Path(__file__).resolve().parent.parent


class SelfHostingTests(unittest.TestCase):
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
        approval = (
            PROCESS_ROOT / ".github" / "workflows" / "release-approval.yml"
        ).read_text(encoding="utf-8")
        generator = (
            PROCESS_ROOT / ".github" / "workflows" / "release-pr.yml"
        ).read_text(encoding="utf-8")
        ci = (
            PROCESS_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", candidate)
        self.assertNotIn("pull_request:", candidate)
        self.assertIn("Verify exact unpublished checkpoint", candidate)
        self.assertIn("Preserve the exact verified checkpoint and lifecycle", candidate)
        self.assertNotIn("processctl change review start", candidate)
        self.assertNotIn("git push", candidate)
        self.assertNotIn("gh pr create", candidate)
        self.assertIn("engineering-process-review-required", candidate)
        self.assertIn("consumer-selected reviewer host", candidate)
        self.assertIn('publicationWorkflow: $publicationWorkflow', candidate)
        self.assertIn('completionEvidenceEncoding: $completionEvidenceEncoding', candidate)
        self.assertNotIn("processctl change review start", approval)
        self.assertNotIn("--review-report", approval)
        self.assertNotIn("processctl change finish", approval)
        self.assertIn("completion_evidence_gzip_base64", approval)
        self.assertIn("verification/decode_completion_evidence.py", approval)
        self.assertIn("processctl evidence validate", approval)
        self.assertIn("publication validate-evidence-source", approval)
        self.assertLess(
            approval.index("publication validate-evidence-source"),
            approval.index("git push origin"),
        )
        self.assertLess(approval.index("git push origin"), approval.index("gh pr create"))
        self.assertNotIn("gh pr ready", approval)
        self.assertIn("reconcile_completed_release.py", approval)
        self.assertIn("--limit 2", approval)
        self.assertIn("if test \"$action\" = existing", approval)
        self.assertIn("Project-specific: Completion evidence", approval)
        self.assertIn("release-changes/*.json", generator)
        self.assertIn('".process/process.lock"', generator)
        self.assertIn('"requirements/process.in"', generator)
        self.assertIn('"requirements/process.txt"', generator)
        self.assertIn("verification/classify_release_preparation.py", generator)
        self.assertIn('".github/workflows/release-pr.yml"', generator)
        self.assertNotIn("gh pr create", generator)
        self.assertNotIn("gh pr ready", generator)
        self.assertNotIn("git push", generator)
        self.assertIn("source.bundle", generator)
        self.assertIn("unpublished-release-candidate", generator)
        self.assertIn("Detect pending release changes", generator)
        self.assertIn("No pending release changes", generator)
        self.assertIn('gh workflow run release-candidate.yml', generator)
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

    def test_renovate_cannot_publish_before_a_completed_lifecycle(self):
        renovate = json.loads(
            (PROCESS_ROOT / ".github" / "renovate.json").read_text(encoding="utf-8")
        )

        self.assertFalse(renovate["enabled"])
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
        self.assertIn('if test -n "$run_id"; then', evidence_recovery)
        self.assertIn(
            'test "$(gh release view "$TAG" --json isDraft --jq .isDraft)" = false',
            evidence_recovery,
        )
        self.assertIn(
            'test "$(gh release view "$TAG" --json publishedAt --jq .publishedAt)" != null',
            evidence_recovery,
        )
        self.assertIn('gh release verify "$TAG"', evidence_recovery)
        self.assertIn(
            'gh release download "$TAG" --pattern "$EVIDENCE_ASSET"',
            evidence_recovery,
        )
        self.assertLess(
            evidence_recovery.index('gh run download "$run_id"'),
            evidence_recovery.index('gh release verify "$TAG"'),
        )
        self.assertNotIn("gh release create", evidence_recovery)
        self.assertIn("gh release edit", release)
        for workflow in (release, prepare, publish):
            self.assertIn(".release-controller", workflow)
            self.assertIn("github.workflow_sha", workflow)
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
            "ref: ${{ github.event.pull_request.head.sha || github.sha }}",
            workflow,
        )
        self.assertIn("workflow_dispatch:", workflow)
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
        self.assertNotIn('"--status-handle"', managed.read_text(encoding="utf-8"))
        self.assertIn('"--status-handle"', template.read_text(encoding="utf-8"))
        self.assertTrue(
            managed_windows_helper.read_text(encoding="utf-8").startswith(marker)
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
