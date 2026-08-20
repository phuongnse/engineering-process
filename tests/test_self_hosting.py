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
        self.assertIn("workflow_dispatch:", prepare)
        self.assertIn("isDraft", prepare)
        self.assertIn("gh release upload", prepare)
        self.assertIn("expected-release-assets.txt", prepare)
        self.assertIn("release:\n    types: [published]", publish)
        self.assertNotIn("gh release upload", publish)
        self.assertGreaterEqual(publish.count("gh release verify"), 2)
        self.assertGreaterEqual(publish.count("gh release verify-asset"), 2)
        self.assertGreaterEqual(publish.count("attestations: read"), 2)
        self.assertGreaterEqual(publish.count("expected-release-assets.txt"), 4)
        self.assertGreaterEqual(publish.count("actual-release-assets.txt"), 4)
        self.assertGreaterEqual(
            publish.count("gh release view \"$GITHUB_REF_NAME\" --json assets"),
            2,
        )
        self.assertIn(
            "Revalidate immutable release immediately before publication", publish
        )

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

    def test_verification_workflow_binds_and_uploads_bounded_matrix_evidence(self):
        workflow = (
            PROCESS_ROOT
            / ".github"
            / "workflows"
            / "engineering-process-verification.yml"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "ref: ${{ github.event.pull_request.head.sha || github.sha }}",
            workflow,
        )
        self.assertIn("verification/generate_ci_evidence.py", workflow)
        self.assertIn("python -m pip install", workflow)
        for requirement in (
            "engineering_process/requirements-runtime.txt",
            "engineering_process/requirements-dev.txt",
            "engineering_process/requirements-build.txt",
        ):
            self.assertIn(requirement, workflow)
        self.assertIn(
            'python verification/verify_distribution.py --output "$RUNNER_TEMP/dist"',
            workflow,
        )
        self.assertNotIn('".[dev]"', workflow)
        self.assertIn(
            '--expected-checkpoint "$VERIFICATION_CHECKPOINT"', workflow
        )
        self.assertIn(
            '--comparison-base "$VERIFICATION_COMPARISON_BASE"', workflow
        )
        self.assertIn('--workflow-sha "$VERIFICATION_WORKFLOW_SHA"', workflow)
        self.assertIn(
            "VERIFICATION_WORKFLOW_SHA: ${{ github.workflow_sha }}", workflow
        )
        self.assertIn(
            "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
            workflow,
        )
        self.assertIn("if-no-files-found: error", workflow)
        self.assertIn("retention-days: 90", workflow)
        self.assertIn("engineering-process-verification-evidence-", workflow)

    def test_repository_policy_uses_stable_meaningful_check_contexts(self):
        policy = json.loads(
            (
                PROCESS_ROOT / ".process" / "repository-governance.json"
            ).read_text(encoding="utf-8")
        )
        verification = (
            PROCESS_ROOT
            / ".github"
            / "workflows"
            / "engineering-process-verification.yml"
        ).read_text(encoding="utf-8")
        metadata_policy = (
            PROCESS_ROOT
            / ".github"
            / "workflows"
            / "change-metadata-policy.yml"
        ).read_text(encoding="utf-8")

        self.assertEqual(
            ["Change metadata policy", "Merge eligibility"],
            policy["defaultBranch"]["requiredChecks"],
        )
        self.assertIn("name: Engineering process verification", verification)
        self.assertIn(
            "name: Distribution verification "
            "(${{ matrix.os }}, Python ${{ matrix.python-version }})",
            verification,
        )
        self.assertIn("name: Merge eligibility", verification)
        self.assertIn("needs: [distribution-verification]", verification)
        self.assertIn(
            'if [ "$DISTRIBUTION_VERIFICATION_RESULT" != "success" ]',
            verification,
        )
        self.assertNotIn("publication validate-pr", verification)
        self.assertIn("name: Change metadata policy", metadata_policy)
        for event in (
            "opened",
            "synchronize",
            "reopened",
            "edited",
            "ready_for_review",
            "converted_to_draft",
        ):
            self.assertIn(f"- {event}", metadata_policy)
        self.assertIn(
            "pip install --require-hashes -r requirements/process.txt",
            metadata_policy,
        )
        self.assertIn(
            '--body-file "$RUNNER_TEMP/change-description.md"', metadata_policy
        )
        self.assertNotIn(
            "engineering_process/requirements-dev.txt", metadata_policy
        )

    def test_workflow_metadata_is_explicit_and_meaningful(self):
        generic_display_names = {
            "Build",
            "Check",
            "CI",
            "Deploy",
            "Gate",
            "Prepare",
            "Publish",
            "Release",
            "Test",
            "Verify",
        }
        workflow_root = PROCESS_ROOT / ".github" / "workflows"
        for path in sorted(workflow_root.glob("*.yml")):
            with self.subTest(workflow=path.name):
                self.assertRegex(path.stem, r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
                text = path.read_text(encoding="utf-8")
                workflow_name = re.search(r"(?m)^name: (?P<name>\S.*)$", text)
                self.assertIsNotNone(workflow_name)
                assert workflow_name is not None
                self.assertNotIn(
                    workflow_name.group("name"), generic_display_names
                )
                self.assertRegex(text, r"(?m)^run-name: \S.*$")

                jobs_start = text.find("\njobs:\n")
                self.assertNotEqual(-1, jobs_start)
                jobs_text = text[jobs_start + 1 :]
                jobs = list(
                    re.finditer(
                        r"(?m)^  (?P<id>[a-z][a-z0-9]*(?:-[a-z0-9]+)*):\s*$",
                        jobs_text,
                    )
                )
                self.assertTrue(jobs)
                for index, match in enumerate(jobs):
                    job_id = match.group("id")
                    end = (
                        jobs[index + 1].start()
                        if index + 1 < len(jobs)
                        else len(jobs_text)
                    )
                    job = jobs_text[match.start() : end]
                    with self.subTest(workflow=path.name, job=job_id):
                        job_name = re.search(r"(?m)^    name: (?P<name>\S.*)$", job)
                        self.assertIsNotNone(job_name)
                        assert job_name is not None
                        self.assertNotIn(
                            job_name.group("name"), generic_display_names
                        )
                        self.assertNotRegex(job, r"(?m)^      - (?:uses|run):")
                        step_names = re.findall(
                            r"(?m)^      - name: (?P<name>\S.*)$", job
                        )
                        self.assertTrue(step_names)
                        for step_name in step_names:
                            self.assertNotIn(step_name, generic_display_names)
                        for step_id in re.findall(
                            r"(?m)^        id: (?P<id>\S+)$", job
                        ):
                            self.assertRegex(
                                step_id,
                                r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$",
                            )
                lines = text.splitlines()
                for line_index, line in enumerate(lines):
                    env_match = re.match(r"^(?P<indent> +)env:\s*$", line)
                    if env_match is None:
                        continue
                    env_indent = len(env_match.group("indent"))
                    for candidate in lines[line_index + 1 :]:
                        if not candidate.strip():
                            continue
                        candidate_indent = len(candidate) - len(
                            candidate.lstrip(" ")
                        )
                        if candidate_indent <= env_indent:
                            break
                        if candidate_indent != env_indent + 2:
                            continue
                        env_name = candidate.strip().partition(":")[0]
                        self.assertRegex(env_name, r"^[A-Z][A-Z0-9_]*$")

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

    def test_adoption_sources_preserve_the_authority_boundary(self):
        managed = PROCESS_ROOT / ".process" / "adopt-process.py"
        template = PROCESS_ROOT / "templates" / "adopt-process.py"
        managed_windows_helper = (
            PROCESS_ROOT / ".process" / "adopt-process-windows-job.py"
        )
        template_windows_helper = (
            PROCESS_ROOT / "templates" / "adopt-process-windows-job.py"
        )
        windows_helper = PROCESS_ROOT / "engineering_process" / "_windows_job.py"

        self.assertEqual(template.read_bytes(), managed.read_bytes())
        self.assertTrue(
            template.read_text(encoding="utf-8").startswith(
                "# Managed by engineering-process; do not edit.\n"
            )
        )
        self.assertTrue(
            managed_windows_helper.read_text(encoding="utf-8").startswith(
                "# Managed by engineering-process; do not edit.\n"
            )
        )
        self.assertEqual(
            windows_helper.read_bytes(), template_windows_helper.read_bytes()
        )

    def test_distribution_never_packages_managed_skill_copies(self):
        pyproject = (PROCESS_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        manifest = (PROCESS_ROOT / "MANIFEST.in").read_text(encoding="utf-8")

        self.assertIn('"process_assets/skills/', pyproject)
        self.assertNotIn('".agents/skills/', pyproject)
        self.assertIn('"ADOPTION_ADAPTER.md"', pyproject)
        self.assertIn('"ENVIRONMENT_CONTRACT.md"', pyproject)
        self.assertIn('"GITHUB_REPOSITORY_ADAPTER.md"', pyproject)
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

    def test_publish_fails_closed_at_the_n_minus_one_and_hash_boundaries(self):
        workflow = (
            PROCESS_ROOT / ".github" / "workflows" / "publish.yml"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'process-authority/bin/processctl" evidence validate', workflow
        )
        self.assertNotIn("bootstrap validator", workflow)
        self.assertNotIn("authority_version=", workflow)
        self.assertIn("pip install --require-hashes", workflow)
        self.assertIn("engineering_process/requirements-release.txt", workflow)
        self.assertIn("--no-deps", workflow)

    def test_release_guide_preserves_the_non_resolving_build_boundary(self):
        guide = (PROCESS_ROOT / "RELEASING.md").read_text(encoding="utf-8")
        verifier = (
            PROCESS_ROOT / "engineering_process" / "distribution_verify.py"
        ).read_text(encoding="utf-8")

        self.assertIn("--require-hashes", guide)
        self.assertIn("--no-isolation", guide)
        self.assertIn("no network\n   resolution", guide)
        self.assertIn('"--no-isolation"', verifier)


if __name__ == "__main__":
    unittest.main()
