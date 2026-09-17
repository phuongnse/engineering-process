from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engineering_process.cli import (
    build_parser,
    command_change_review_start,
    command_change_verify,
    main,
)


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
                "artifact",
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

    def test_verify_accepts_an_explicit_diagnostic_check(self) -> None:
        args = build_parser().parse_args(
            ["verify", "--profile", "rust", "--check-position", "2", "--progress"]
        )
        self.assertEqual("rust", args.profile)
        self.assertEqual(2, args.check_position)
        self.assertTrue(args.progress)

    def test_change_verify_supports_remaining_and_explain_commands(self) -> None:
        remaining = build_parser().parse_args(
            ["change", "verify", "--change-id", "sample-change", "--remaining"]
        )
        self.assertTrue(remaining.remaining)
        self.assertIsNone(remaining.profile)
        explain = build_parser().parse_args(
            ["change", "explain", "--change-id", "sample-change"]
        )
        self.assertEqual("sample-change", explain.change_id)
        affected = build_parser().parse_args(
            ["change", "verify", "--change-id", "sample-change", "--affected", "--affected-profile", "development"]
        )
        self.assertTrue(affected.affected)
        self.assertEqual(["development"], affected.affected_profile)
        self.assertTrue(
            build_parser().parse_args(
                ["change", "verify", "--change-id", "sample-change", "--remaining", "--progress"]
            ).progress
        )
        impact = build_parser().parse_args(
            ["change", "explain", "--change-id", "sample-change", "--impact", "--profile", "development"]
        )
        self.assertTrue(impact.impact)
        self.assertEqual("development", impact.profile)

    def test_progress_stays_on_stderr_while_json_result_remains_parseable(self) -> None:
        checkpoint = {
            "head": "a" * 40,
            "fingerprint": "sha256:" + "b" * 64,
            "fileCount": 1,
            "byteCount": 1,
        }

        def fake_run_profile(*_args, progress_callback=None, **_kwargs):
            if progress_callback is not None:
                progress_callback(
                    {
                        "phase": "running",
                        "status": "running",
                        "profile": "development",
                        "check": "unit",
                        "position": 1,
                        "elapsedMs": 10,
                        "timeoutSeconds": 60,
                        "lastObservedAt": "2026-09-17T00:00:00Z",
                        "runnerActive": True,
                        "runnerResponsive": True,
                        "progress": "unknown",
                        "outputBytes": 0,
                        "stdout": "secret-child-output",
                    }
                )
            return {
                "profile": "development",
                "status": "passed",
                "durationMs": 10,
                "scope": {"kind": "profile"},
                "checks": [],
            }

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), patch(
            "engineering_process.cli.load_project",
            return_value={"profiles": {"development": []}},
        ), patch(
            "engineering_process.cli.repository_snapshot",
            side_effect=[checkpoint, checkpoint],
        ), patch(
            "engineering_process.cli.run_profile",
            side_effect=fake_run_profile,
        ):
            code = main(
                ["verify", "--profile", "development", "--progress", "--json"]
            )

        self.assertEqual(0, code)
        result = json.loads(stdout.getvalue())
        self.assertEqual("passed", result["status"])
        progress = json.loads(stderr.getvalue().strip())
        self.assertEqual("verification progress", progress["command"])
        self.assertEqual("unknown", progress["progress"])
        self.assertNotIn("stdout", progress)
        self.assertNotIn("secret-child-output", stderr.getvalue())

    def test_remaining_command_propagates_failed_execution(self) -> None:
        state = {
            "changeId": "sample-change",
            "phase": "implementing",
            "cycle": 1,
            "verification": {
                "development": {
                    "status": "failed",
                    "checks": [],
                }
            },
        }
        selection = {
            "executeProfiles": ["development"],
            "reuseProfiles": [],
            "inapplicableProfiles": [],
        }
        args = argparse.Namespace(
            process_root=ROOT,
            project_root=ROOT,
            change_id="sample-change",
            remaining=True,
            profile=None,
        )
        with patch("engineering_process.cli.load_project", return_value={}), patch(
            "engineering_process.cli.verify_remaining",
            return_value=(state, selection),
        ):
            result, code = command_change_verify(args)
        self.assertEqual(1, code)
        self.assertEqual("failed", result["status"])
        self.assertEqual(["development"], result["failures"])

    def test_explicit_change_verify_reports_failed_profile_as_failed(self) -> None:
        state = {
            "changeId": "sample-change",
            "phase": "implementing",
            "cycle": 1,
        }
        args = argparse.Namespace(
            process_root=ROOT,
            project_root=ROOT,
            change_id="sample-change",
            remaining=False,
            profile="development",
            affected=False,
            affected_profile=[],
            progress=False,
        )
        with patch("engineering_process.cli.load_project", return_value={}), patch(
            "engineering_process.cli.verify_change",
            return_value=(state, {"status": "failed"}),
        ):
            result, code = command_change_verify(args)
        self.assertEqual(1, code)
        self.assertEqual("failed", result["status"])
        self.assertEqual("failed", result["profileStatus"])

    def test_remaining_command_fails_when_lifecycle_is_still_incomplete(self) -> None:
        state = {
            "changeId": "sample-change",
            "phase": "implementing",
            "cycle": 1,
            "verification": {},
        }
        selection = {
            "executeProfiles": [],
            "reuseProfiles": [],
            "inapplicableProfiles": [],
        }
        args = argparse.Namespace(
            process_root=ROOT,
            project_root=ROOT,
            change_id="sample-change",
            remaining=True,
            profile=None,
        )
        with patch("engineering_process.cli.load_project", return_value={}), patch(
            "engineering_process.cli.verify_remaining",
            return_value=(state, selection),
        ):
            result, code = command_change_verify(args)
        self.assertEqual(1, code)
        self.assertEqual("failed", result["status"])

    def test_remaining_command_requires_every_required_profile_in_verified_state(self) -> None:
        state = {
            "changeId": "sample-change",
            "phase": "verified",
            "cycle": 1,
            "contract": {"document": {"requiredProfiles": ["development", "review"]}},
            "verification": {
                "development": {"status": "passed", "checks": []},
            },
        }
        selection = {
            "executeProfiles": [],
            "reuseProfiles": [],
            "inapplicableProfiles": [],
        }
        args = argparse.Namespace(
            process_root=ROOT,
            project_root=ROOT,
            change_id="sample-change",
            remaining=True,
            profile=None,
        )
        with patch("engineering_process.cli.load_project", return_value={}), patch(
            "engineering_process.cli.verify_remaining",
            return_value=(state, selection),
        ):
            result, code = command_change_verify(args)
        self.assertEqual(1, code)
        self.assertEqual("failed", result["status"])

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

    def test_contract_validate_rejects_a_non_current_plan(self) -> None:
        non_current = {
            "schemaVersion": 2,
            "changeId": "sample-change",
            "contractDigest": "sha256:" + "0" * 64,
            "approach": "Implement the accepted plan.",
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
            path.write_text(json.dumps(non_current), encoding="utf-8")
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
        self.assertEqual(2, code)
        self.assertEqual("failed", json.loads(output.getvalue())["status"])

    def test_project_validate_reports_resolved_production_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            process_dir = project_root / ".process"
            process_dir.mkdir(parents=True)
            project = json.loads(
                (ROOT / ".process" / "project.json").read_text(encoding="utf-8")
            )
            project["schemaVersion"] = 1
            (process_dir / "project.json").write_text(
                json.dumps(project), encoding="utf-8"
            )
            (process_dir / "readiness.json").write_bytes(
                (ROOT / ".process" / "readiness.json").read_bytes()
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "project",
                        "validate",
                        "--project-root",
                        str(project_root),
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
            "comparisonBaseCommit": "a" * 40,
            "reviewAssignment": {"reportSchemaVersion": 1},
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
            review_command="start",
        )
        with patch("engineering_process.cli.start_review", return_value=state):
            result, code = command_change_review_start(args)
            self.assertEqual("a" * 40, result["comparisonBaseCommit"])
        self.assertEqual(0, code)
        self.assertEqual(["profile-failed"], result["processSignals"])


if __name__ == "__main__":
    unittest.main()
