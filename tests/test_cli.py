from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import unittest

from engineering_process.cli import build_parser, main


ROOT = Path(__file__).resolve().parent.parent


class CliTests(unittest.TestCase):
    def test_public_command_surface_is_small(self) -> None:
        parser = build_parser()
        subparsers = next(
            action for action in parser._actions if action.__class__.__name__ == "_SubParsersAction"
        )
        self.assertEqual(
            {
                "adoption",
                "change",
                "contract",
                "doctor",
                "lock",
                "project",
                "publication",
                "release",
                "setup",
                "skills",
                "verify",
            },
            set(subparsers.choices),
        )

    def test_skills_validate_emits_machine_readable_result(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                [
                    "skills",
                    "validate",
                    "--process-root",
                    str(ROOT),
                    "--root",
                    str(ROOT / "process_assets" / "skills"),
                    "--json",
                ]
            )
        self.assertEqual(0, code)
        result = json.loads(output.getvalue())
        self.assertEqual("passed", result["status"])
        self.assertEqual(len(result["skills"]), result["count"])
        self.assertIn("production-engineering", result["skills"])

    def test_contract_error_returns_nonzero_json(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                [
                    "contract",
                    "validate",
                    "--process-root",
                    str(ROOT),
                    "--kind",
                    "change",
                    str(ROOT / "release.json"),
                    "--json",
                ]
            )
        self.assertEqual(2, code)
        result = json.loads(output.getvalue())
        self.assertEqual("failed", result["status"])

    def test_project_validate_reports_resolved_production_readiness(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                [
                    "project",
                    "validate",
                    "--project-root",
                    str(ROOT),
                    "--process-root",
                    str(ROOT),
                    "--json",
                ]
            )
        self.assertEqual(0, code)
        result = json.loads(output.getvalue())
        self.assertEqual("production", result["readiness"]["target"])
        self.assertEqual("production", result["readiness"]["stage"])
        self.assertEqual([{"id": "library-cli", "version": 1}], result["readiness"]["packs"])
        self.assertEqual([], result["readiness"]["plannedCapabilities"])
        self.assertIn("distribution-integrity", result["readiness"]["capabilities"])


if __name__ == "__main__":
    unittest.main()
