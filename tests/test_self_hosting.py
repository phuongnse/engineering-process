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
                ".process/process.lock",
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

    def test_managed_adoption_runner_matches_packaged_template(self):
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


if __name__ == "__main__":
    unittest.main()
