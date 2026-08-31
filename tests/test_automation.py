from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parent.parent
ACTIVE_PROCESS_PIN = re.compile(
    r"^engineering-process==([^\s\\]+)\s*$", re.MULTILINE
)


class AutomationTests(unittest.TestCase):
    def test_renovate_process_rule_materializes_the_complete_adoption(self) -> None:
        config = json.loads((ROOT / ".github" / "renovate.json").read_text(encoding="utf-8"))
        rules = [
            rule
            for rule in config["packageRules"]
            if "engineering-process" in rule.get("matchPackageNames", [])
        ]
        self.assertEqual(1, len(rules))
        rule = rules[0]
        self.assertTrue(config["draftPR"])
        self.assertTrue(rule["enabled"])
        self.assertTrue(rule["draftPR"])
        self.assertEqual("automation/renovate/", config["branchPrefix"])
        self.assertEqual(
            ["python .process/adopt-process.py --project-root . --requirements-lock requirements/process.txt"],
            rule["postUpgradeTasks"]["commands"],
        )
        self.assertEqual("update", rule["postUpgradeTasks"]["executionMode"])
        self.assertEqual(
            [
                ".agents/skills/**",
                ".github/PULL_REQUEST_TEMPLATE.md",
                ".process/adopt-process.py",
                ".process/automation.json",
                ".process/adopt-process-windows-job.py",
                ".process/adoption-migrations/**",
                ".process/process.lock",
                ".process/project.json",
                "AGENTS.md",
            ],
            rule["postUpgradeTasks"]["fileFilters"],
        )

    def test_release_dispatches_the_published_version_to_the_control_plane(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "publish.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("engineering-process-published", workflow)
        self.assertIn("repos/phuongnse/renovate-ops/dispatches", workflow)
        self.assertIn("dispatch-adoption:", workflow)
        self.assertIn("needs: [metadata, publish]", workflow)
        self.assertIn("Verify PyPI exposes the exact built hashes", workflow)
        self.assertIn("SOURCE_DATE_EPOCH", workflow)
        self.assertIn("verification/normalize_sdist.py", workflow)
        self.assertIn("points to a different commit", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("release_sha:", workflow)
        self.assertIn('git merge-base --is-ancestor "$RELEASE_SOURCE_SHA" origin/main', workflow)
        self.assertIn("needs.metadata.outputs.source_sha", workflow)
        self.assertNotIn('--target "$GITHUB_SHA"', workflow)
        self.assertEqual(
            3,
            workflow.count(
                'path.suffix == ".whl" or path.name.endswith(".tar.gz")'
            ),
        )
        self.assertIn("for file in dist/*.whl dist/*.tar.gz; do", workflow)
        self.assertNotIn("for file in dist/*; do", workflow)
        publish_job = workflow.split("  publish:\n", maxsplit=1)[1].split(
            "\n  dispatch-adoption:\n", maxsplit=1
        )[0]
        self.assertIn("id: release-token", publish_job)
        self.assertIn("repositories: engineering-process", publish_job)
        self.assertIn("permission-contents: write", publish_job)
        self.assertIn(
            "GH_TOKEN: ${{ steps.release-token.outputs.token }}", publish_job
        )
        self.assertNotIn("GH_TOKEN: ${{ github.token }}", publish_job)
        self.assertIn('"repos/$GITHUB_REPOSITORY/git/refs"', publish_job)
        self.assertIn('-f ref="refs/tags/$RELEASE_TAG"', publish_job)
        self.assertIn('-f sha="${{ needs.metadata.outputs.source_sha }}"', publish_job)
        self.assertIn("--verify-tag", publish_job)
        self.assertNotIn("--target", publish_job)
        trusted_checkout = workflow.index("          ref: main")
        preflight = workflow.index("name: Authorize release source from trusted main")
        source_checkout = workflow.index("ref: ${{ steps.release.outputs.source_sha }}")
        editable_install = workflow.index(
            "python -m pip install -r engineering_process/requirements-runtime.txt "
            "--editable ."
        )
        self.assertLess(trusted_checkout, preflight)
        self.assertLess(preflight, source_checkout)
        self.assertLess(source_checkout, editable_install)

    def test_ci_checks_the_adopted_hash_locked_distribution_separately(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        policy_job = "  policy-verification:\n" + workflow.split(
            "  policy-verification:\n", maxsplit=1
        )[1].split("\n  adopted-process:\n", maxsplit=1)[0]
        self.assertEqual(
            "  policy-verification:\n"
            "    name: policy-verification\n"
            "    if: github.event_name == 'pull_request'\n"
            "    permissions:\n"
            "      contents: read\n"
            "      pull-requests: read\n"
            "    uses: phuongnse/renovate-ops/.github/workflows/"
            "policy-verification.yml@"
            "5fb53c2295c0f62c29d34c8141121b71198769f4\n",
            policy_job,
        )
        self.assertIn("name: verify (${{ matrix.os }}, ${{ matrix.python }})", workflow)
        self.assertIn("adopted-process:", workflow)
        adopted_job = workflow.split("  adopted-process:\n", maxsplit=1)[1].split(
            "\n  test:\n", maxsplit=1
        )[0]
        self.assertIn("--require-hashes", adopted_job)
        self.assertIn("Install exact producer dependencies for doctor", adopted_job)
        for requirements in (
            "engineering_process/requirements-runtime.txt",
            "engineering_process/requirements-dev.txt",
            "engineering_process/requirements-build.txt",
        ):
            self.assertIn(f"-r {requirements}", adopted_job)
        producer_install = adopted_job.index(
            "Install exact producer dependencies for doctor"
        )
        adoption_check = adopted_job.index("processctl adoption check")
        doctor = adopted_job.index("processctl doctor --project-root .")
        self.assertLess(producer_install, adoption_check)
        self.assertLess(adoption_check, doctor)

    def test_readiness_sidecar_preserves_the_adopted_authority_bootstrap(self) -> None:
        project = json.loads((ROOT / ".process" / "project.json").read_text(encoding="utf-8"))
        readiness = json.loads((ROOT / ".process" / "readiness.json").read_text(encoding="utf-8"))
        self.assertNotIn("readiness", project)
        self.assertEqual("production", readiness["target"])
        self.assertEqual("production", readiness["stage"])
        self.assertEqual([{"id": "library-cli", "version": 1}], readiness["packs"])
        adopted = json.loads(
            (ROOT / ".process" / "process.lock").read_text(encoding="utf-8")
        )
        requirements = (ROOT / "requirements" / "process.in").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            [adopted["process"]["version"]], ACTIVE_PROCESS_PIN.findall(requirements)
        )
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("processctl adoption check", workflow)
        self.assertIn("processctl doctor --project-root .", workflow)

    def test_self_adoption_pin_parser_ignores_comments_and_prefix_collisions(self) -> None:
        self.assertEqual(
            ["1.0.10"], ACTIVE_PROCESS_PIN.findall("engineering-process==1.0.10\n")
        )
        self.assertEqual(
            [], ACTIVE_PROCESS_PIN.findall("# engineering-process==1.0.1\n")
        )

    def test_external_actions_are_immutably_pinned(self) -> None:
        for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
            for reference in re.findall(r"uses:\s*([^\s#]+)", workflow.read_text(encoding="utf-8")):
                if reference.startswith("./"):
                    continue
                self.assertRegex(reference, r"^[^@]+@[0-9a-f]{40}$", workflow.name)

    def test_consumer_improvement_issue_form_is_bounded_and_non_automated(self) -> None:
        form = (ROOT / ".github" / "ISSUE_TEMPLATE" / "consumer-process-improvement.yml").read_text(encoding="utf-8")
        self.assertEqual(
            {
                "authority",
                "blocking",
                "consumer",
                "disclosure",
                "evidence",
                "expected",
                "incident_type",
                "mitigation",
                "observed",
                "reusable",
                "stable_key",
            },
            set(re.findall(r"^    id: ([a-z_]+)$", form, re.MULTILINE)),
        )
        self.assertGreaterEqual(form.count("required: true"), 13)
        self.assertIn('title: "[consumer-process][CONSUMER][PROCESS-VERSION][INVARIANT] "', form)
        self.assertIn("searched open engineering-process issues for the complete stable key", form)
        for forbidden in ("secrets", "credentials", "raw private logs", "media", "private source"):
            self.assertIn(forbidden, form)
        workflows = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / ".github" / "workflows").glob("*.yml")
        )
        self.assertNotIn("consumer-process-improvement", workflows)


if __name__ == "__main__":
    unittest.main()
