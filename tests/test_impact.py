import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engineering_process.contracts import (
    Check,
    ContractError,
    ImpactComponent,
    Project,
    ProjectImpact,
)
from engineering_process.impact import IMPACT_FILE_ENV, plan_profile
from engineering_process.runner import run_profile


class ImpactTests(unittest.TestCase):
    def initialize_repository(self, root: Path) -> str:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "process-test@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Process Test"],
            cwd=root,
            check=True,
        )
        (root / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def project(self) -> Project:
        return Project(
            identifier="sample",
            profiles={
                "development": (
                    Check(
                        identifier="always",
                        run=(sys.executable, "-c", "pass"),
                        timeout_seconds=10,
                        working_directory=".",
                    ),
                    Check(
                        identifier="api-check",
                        run=(sys.executable, "-c", "pass"),
                        timeout_seconds=10,
                        working_directory=".",
                        components=("api",),
                    ),
                    Check(
                        identifier="frontend-check",
                        run=(sys.executable, "-c", "pass"),
                        timeout_seconds=10,
                        working_directory=".",
                        components=("frontend",),
                    ),
                    Check(
                        identifier="docs-check",
                        run=(sys.executable, "-c", "pass"),
                        timeout_seconds=10,
                        working_directory=".",
                        components=("docs",),
                    ),
                )
            },
            impact=ProjectImpact(
                base_refs=("main",),
                unmatched_paths="all-scoped-checks",
                components={
                    "api": ImpactComponent(
                        identifier="api",
                        paths=("openapi.json", "src/api/**"),
                        affects=("frontend",),
                    ),
                    "docs": ImpactComponent(
                        identifier="docs",
                        paths=("**/*.md",),
                        affects=(),
                    ),
                    "frontend": ImpactComponent(
                        identifier="frontend",
                        paths=("frontend/**",),
                        affects=(),
                    ),
                },
            ),
        )

    def test_selects_transitive_components_and_records_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.initialize_repository(root)
            (root / "src" / "api").mkdir(parents=True)
            (root / "src" / "api" / "endpoint.py").write_text(
                "VALUE = 1\n", encoding="utf-8"
            )

            plan = plan_profile(root, self.project(), "development", base_ref=base)

            self.assertEqual(
                [check.identifier for check in plan.checks],
                ["always", "api-check", "frontend-check"],
            )
            self.assertEqual(plan.evidence["directlyChangedComponents"], ["api"])
            self.assertEqual(
                plan.evidence["affectedComponents"], ["api", "frontend"]
            )
            self.assertEqual(plan.evidence["unmatchedPaths"], [])
            self.assertEqual(plan.evidence["skippedCheckIds"], ["docs-check"])

    def test_unknown_path_fails_safe_to_every_scoped_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.initialize_repository(root)
            (root / "unknown.bin").write_bytes(b"unknown")

            plan = plan_profile(root, self.project(), "development", base_ref=base)

            self.assertEqual(
                [check.identifier for check in plan.checks],
                ["always", "api-check", "frontend-check", "docs-check"],
            )
            self.assertEqual(plan.evidence["unmatchedPaths"], ["unknown.bin"])
            self.assertTrue(
                all(
                    item["reason"] == "unmatched-path-fallback"
                    for item in plan.evidence["checkSelection"]
                    if item["id"] != "always"
                )
            )

    def test_no_changed_paths_runs_only_unscoped_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.initialize_repository(root)

            plan = plan_profile(root, self.project(), "development", base_ref=base)

            self.assertEqual(
                [check.identifier for check in plan.checks],
                ["always"],
            )
            self.assertEqual(
                plan.evidence["skippedCheckIds"],
                ["api-check", "frontend-check", "docs-check"],
            )

    def test_manifest_without_impact_deliberately_runs_full_profile(self):
        project = Project(
            identifier="sample",
            profiles={
                "development": (
                    Check(
                        identifier="unit",
                        run=(sys.executable, "-c", "pass"),
                        timeout_seconds=10,
                        working_directory=".",
                    ),
                )
            },
        )

        plan = plan_profile(Path("/not/a/repository"), project, "development")

        self.assertEqual(plan.evidence["mode"], "full-profile")
        self.assertEqual(plan.evidence["selectedCheckIds"], ["unit"])

    def test_explicit_missing_base_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.initialize_repository(root)

            with self.assertRaisesRegex(ContractError, "explicit base ref is unavailable"):
                plan_profile(
                    root,
                    self.project(),
                    "development",
                    base_ref="missing-ref",
                )

    def test_selected_command_receives_process_owned_impact_document(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.initialize_repository(root)
            (root / "frontend").mkdir()
            (root / "frontend" / "app.ts").write_text("export {};\n", encoding="utf-8")
            project = Project(
                identifier="sample",
                profiles={
                    "development": (
                        Check(
                            identifier="inspect-impact",
                            run=(
                                sys.executable,
                                "-c",
                                "import json, os; "
                                f"p=os.environ[{IMPACT_FILE_ENV!r}]; "
                                "d=json.load(open(p, encoding='utf-8')); "
                                "assert d['affectedComponents'] == ['frontend']",
                            ),
                            timeout_seconds=10,
                            working_directory=".",
                            components=("frontend",),
                        ),
                    )
                },
                impact=ProjectImpact(
                    base_refs=("main",),
                    unmatched_paths="all-scoped-checks",
                    components={
                        "frontend": ImpactComponent(
                            identifier="frontend",
                            paths=("frontend/**",),
                            affects=(),
                        )
                    },
                ),
            )
            previous = os.environ.get(IMPACT_FILE_ENV)
            os.environ[IMPACT_FILE_ENV] = "/untrusted/host/value"
            try:
                report = run_profile(root, project, "development", base_ref=base)
            finally:
                if previous is None:
                    os.environ.pop(IMPACT_FILE_ENV, None)
                else:
                    os.environ[IMPACT_FILE_ENV] = previous

            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["impact"]["selectedCheckIds"], ["inspect-impact"])


if __name__ == "__main__":
    unittest.main()
