from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engineering_process.cli import build_parser, command_change_review_start, main


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

    def test_contract_validate_keeps_the_plan_v4_reader(self) -> None:
        legacy = {
            "schemaVersion": 4,
            "changeId": "legacy-change",
            "contractDigest": "sha256:" + "0" * 64,
            "approach": "Implement the accepted legacy plan.",
            "workItems": [
                {
                    "id": "implementation",
                    "outcome": "Deliver the accepted behavior.",
                    "affectedPaths": ["src/"],
                }
            ],
            "risks": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "contract",
                        "validate",
                        "--process-root",
                        str(ROOT),
                        "--kind",
                        "plan",
                        str(path),
                        "--json",
                    ]
                )
        self.assertEqual(0, code)
        self.assertEqual("passed", json.loads(output.getvalue())["status"])

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

    def test_review_start_reports_bounded_process_signals(self) -> None:
        state = {
            "changeId": "sample-change",
            "phase": "review-pending",
            "cycle": 2,
            "reviewAssignment": {"reportSchemaVersion": 7},
            "history": [
                {"event": "profile-failed", "details": {}},
                {"event": "unrelated-event", "details": {"verdict": "changes-requested"}},
            ],
        }
        args = argparse.Namespace(
            process_root=ROOT,
            project_root=ROOT,
            change_id="sample-change",
            actor="reviewer",
            context="review-context",
            actor_kind="agent",
        )
        with patch("engineering_process.cli.start_review", return_value=state):
            result, code = command_change_review_start(args)
        self.assertEqual(0, code)
        self.assertEqual(["profile-failed"], result["processSignals"])


if __name__ == "__main__":
    unittest.main()
