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
from engineering_process.publication import validate_pull_request


PROCESS_ROOT = Path(__file__).resolve().parent.parent


class SelfHostingTests(unittest.TestCase):
    def test_release_pr_review_keeps_write_authority_out_of_head_code(self):
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

        self.assertIn("contents: read", candidate)
        self.assertNotIn("contents: write", candidate)
        self.assertIn("github.event.pull_request.head.repo.full_name", candidate)
        self.assertIn("workflow_dispatch:", approval)
        self.assertNotIn("workflow_run:", approval)
        self.assertIn("ci_run_id:", approval)
        self.assertIn("release_pr_number:", approval)
        self.assertIn("release_head_sha:", approval)
        self.assertIn("actions/runs/$CI_RUN_ID", approval)
        self.assertIn(
            'test "$ci_event" = pull_request || test "$ci_event" = workflow_dispatch',
            approval,
        )
        self.assertIn('test "$(jq -r .path "$RUNNER_TEMP/ci-run.json")" = .github/workflows/ci.yml', approval)
        self.assertIn("independent-review-$PR_NUMBER-$HEAD_SHA", approval)
        self.assertIn("f22b05f7813d5868f2a728f203a59afa5d6f18d2", approval)
        self.assertIn("release-authorization", approval)
        self.assertIn("statuses: write", approval)
        self.assertIn("release-changes/*.json", generator)
        self.assertIn('".process/process.lock"', generator)
        self.assertIn('"requirements/process.in"', generator)
        self.assertIn('"requirements/process.txt"', generator)
        self.assertIn("verification/classify_release_preparation.py", generator)
        self.assertIn('".github/workflows/release-pr.yml"', generator)
        self.assertIn("gh pr create --draft", generator)
        self.assertIn("--force-with-lease", generator)
        self.assertIn("Detect pending release changes", generator)
        self.assertIn("No pending release changes", generator)
        self.assertIn("actions/create-github-app-token@", generator)
        self.assertIn("RENOVATE_APP_CLIENT_ID", generator)
        self.assertIn("RENOVATE_APP_PRIVATE_KEY", generator)
        self.assertIn("permission-workflows: write", generator)
        self.assertIn("deferred=true", generator)
        self.assertIn("steps.release.outputs.deferred == 'true'", generator)
        self.assertIn("steps.release.outputs.deferred != 'true'", generator)
        self.assertIn("gh release verify", generator)
        self.assertIn("git merge-base --is-ancestor", generator)
        self.assertIn("engineering-process-release-ready", generator)
        self.assertIn('"repos/$GITHUB_REPOSITORY/dispatches"', generator)
        self.assertIn("token: ${{ steps.app-token.outputs.token }}", generator)
        self.assertIn("GH_TOKEN: ${{ steps.app-token.outputs.token }}", generator)
        self.assertIn('gh pr ready "$pr_number" --undo', generator)
        self.assertNotIn('gh workflow run release-candidate.yml', generator)
        self.assertNotIn('gh workflow run ci.yml', generator)
        self.assertIn("github.event_name == 'pull_request'", ci)
        self.assertIn("github.event.pull_request.head.ref == 'automation/release/next'", ci)
        self.assertIn(
            'gh workflow run release-approval.yml --repo "$GITHUB_REPOSITORY" --ref main',
            ci,
        )
        self.assertIn('-f ci_run_id="$GITHUB_RUN_ID"', ci)
        self.assertIn('if test "$GITHUB_EVENT_NAME" = workflow_dispatch; then', ci)
        self.assertIn("python templates/adopt-process.py", ci)
        self.assertIn("--check", ci)
        self.assertNotIn("processctl adoption check", ci)
        self.assertNotIn("python .process/adopt-process.py", ci)
        self.assertIn('gh pr ready "$PR_NUMBER"', approval)

    def test_renovate_generates_complete_draft_adoption_without_merge_authority(self):
        renovate = json.loads(
            (PROCESS_ROOT / ".github" / "renovate.json").read_text(encoding="utf-8")
        )

        self.assertFalse(renovate["automerge"])
        self.assertTrue(renovate["draftPR"])
        self.assertEqual("automation/renovate/", renovate["branchPrefix"])
        self.assertEqual(
            [],
            validate_pull_request(
                title="chore(process): update engineering-process authority",
                body=renovate["prBodyTemplate"],
                branch="automation/renovate/engineering-process-0.x",
                state="draft",
            ),
        )
        authority_rule = next(
            rule
            for rule in renovate["packageRules"]
            if rule.get("matchPackageNames") == ["engineering-process"]
        )
        self.assertTrue(authority_rule["enabled"])
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
        task = renovate["postUpgradeTasks"]
        self.assertEqual("branch", task["executionMode"])
        self.assertEqual(
            [
                "python .process/adopt-process.py --project-root . "
                "--requirements-lock requirements/process.txt"
            ],
            task["commands"],
        )
        self.assertEqual({"python": {}}, task["installTools"])
        self.assertTrue(
            {
                ".agents/skills/**",
                ".process/adopt-process.py",
                ".process/adopt-process-windows-job.py",
                ".process/adoption-migrations/**",
                ".process/process.lock",
                ".process/project.json",
                "requirements/process.in",
                "requirements/process.txt",
            }.issubset(task["fileFilters"])
        )

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

    def test_ci_binds_and_uploads_bounded_matrix_evidence(self):
        workflow = (
            PROCESS_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "ref: ${{ inputs.release_head_sha || github.event.pull_request.head.sha || github.sha }}",
            workflow,
        )
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("reviewed_pr_number: ${{ inputs.release_pr_number }}", workflow)
        self.assertIn("reviewed_head_sha: ${{ inputs.release_head_sha }}", workflow)
        self.assertIn(
            "if: github.event_name == 'pull_request' || github.event_name == 'workflow_dispatch'",
            workflow,
        )
        self.assertIn("PR_BODY: ${{ github.event.pull_request.body }}", workflow)
        self.assertIn("PR_BODY_PATH: ${{ steps.release.outputs.body_path }}", workflow)
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
        self.assertEqual(
            windows_helper.read_bytes(), managed_windows_helper.read_bytes()
        )
        self.assertEqual(
            windows_helper.read_bytes(), template_windows_helper.read_bytes()
        )

    def test_distribution_never_packages_managed_skill_copies(self):
        pyproject = (PROCESS_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        manifest = (PROCESS_ROOT / "MANIFEST.in").read_text(encoding="utf-8")

        self.assertIn('"process_assets/skills/', pyproject)
        self.assertNotIn('".agents/skills/', pyproject)
        self.assertIn('"VERSIONING.md"', pyproject)
        self.assertIn('"templates/adopt-process.py"', pyproject)
        self.assertIn('"templates/adopt-process-windows-job.py"', pyproject)
        self.assertIn(
            '"schemas/adoption-migration.schema.json"', pyproject
        )
        self.assertIn(
            '"schemas/supplemental-verification.schema.json"', pyproject
        )
        self.assertTrue(
            (PROCESS_ROOT / "engineering_process" / "_windows_job.py")
            .read_text(encoding="utf-8")
            .startswith("# Managed by engineering-process; do not edit.\n")
        )
        self.assertIn("prune .agents\n", manifest)
        self.assertIn("prune .process\n", manifest)

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
