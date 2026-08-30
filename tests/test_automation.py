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
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
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

    def test_ci_checks_the_adopted_hash_locked_distribution_separately(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("adopted-process:", workflow)
        self.assertIn("--require-hashes", workflow)
        self.assertIn("Install exact producer dependencies for doctor", workflow)
        for requirements in (
            "engineering_process/requirements-runtime.txt",
            "engineering_process/requirements-dev.txt",
            "engineering_process/requirements-build.txt",
        ):
            self.assertIn(f"-r {requirements}", workflow)
        self.assertIn("processctl adoption check", workflow)
        self.assertIn("processctl doctor --project-root .", workflow)

    def test_external_actions_are_immutably_pinned(self) -> None:
        for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
            for reference in re.findall(r"uses:\s*([^\s#]+)", workflow.read_text(encoding="utf-8")):
                if reference.startswith("./"):
                    continue
                self.assertRegex(reference, r"^[^@]+@[0-9a-f]{40}$", workflow.name)


if __name__ == "__main__":
    unittest.main()
