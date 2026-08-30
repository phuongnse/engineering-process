from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parent.parent


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
        self.assertTrue(rule["enabled"])
        self.assertFalse(rule["automerge"])
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
            "1e3d0d333b62ec92c94ea5c355bbb0cd73024b78\n",
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

    def test_external_actions_are_immutably_pinned(self) -> None:
        for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
            for reference in re.findall(r"uses:\s*([^\s#]+)", workflow.read_text(encoding="utf-8")):
                if reference.startswith("./"):
                    continue
                self.assertRegex(reference, r"^[^@]+@[0-9a-f]{40}$", workflow.name)


if __name__ == "__main__":
    unittest.main()
