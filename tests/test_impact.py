from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from engineering_process.contracts import ProcessError
from engineering_process.impact import impact_unit_lookup, resolve_impact_selection
from engineering_process.lifecycle import resolve_impact_work, verify_affected


ROOT = Path(__file__).resolve().parent.parent


def git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


class ImpactSelectionTests(unittest.TestCase):
    def make_repository(self, root: Path) -> str:
        git(root, "init", "-q")
        git(root, "config", "user.email", "tests@example.invalid")
        git(root, "config", "user.name", "Tests")
        (root / ".gitignore").write_text("/.process/runs/\n", encoding="utf-8")
        (root / "src").mkdir()
        (root / "src" / "app.py").write_text("one\n", encoding="utf-8")
        git(root, "add", ".")
        git(root, "commit", "-qm", "initial")
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()

    def state(self, base: str) -> dict:
        return {
            "changeId": "sample-change",
            "comparisonBaseCommit": base,
            "contract": {
                "document": {"requiredProfiles": ["development"]}
            },
        }

    def lifecycle_state(self, base: str) -> dict:
        return {
            "changeId": "sample-change",
            "phase": "implementing",
            "comparisonBaseCommit": base,
            "cycle": 1,
            "contract": {"document": {"requiredProfiles": ["development"]}},
            "currentImplementation": {
                "actor": {
                    "actorId": "implementation",
                    "contextId": "implementation-context",
                    "kind": "agent",
                }
            },
        }

    def project(self) -> dict:
        return {
            "profiles": {"development": [{"id": "full", "run": ["python"], "timeoutSeconds": 1}]},
            "impactProfiles": {
                "schemaVersion": 1,
                "profiles": {
                    "development": [
                    {
                        "id": "app-tests",
                        "run": ["python", "-c", "pass"],
                        "timeoutSeconds": 10,
                        "paths": ["src/**"],
                    },
                    {
                        "id": "global-tests",
                        "run": ["python", "-c", "pass"],
                        "timeoutSeconds": 10,
                        "scope": "global",
                        "paths": ["**/policy.json"],
                    },
                    ]
                }
            },
        }

    def test_resolves_only_matching_units(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.make_repository(root)
            (root / "src" / "app.py").write_text("two\n", encoding="utf-8")
            selection = resolve_impact_selection(
                root,
                ROOT,
                self.project(),
                self.state(base),
            )
            self.assertEqual("ready", selection["status"])
            self.assertEqual(["app-tests"], [item["id"] for item in selection["selectedUnits"]])
            self.assertEqual([], selection["unresolvedPaths"])

    def test_current_policy_marks_final_assurance_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.make_repository(root)
            (root / "src" / "app.py").write_text("two\n", encoding="utf-8")
            project = self.project()
            project["impactProfiles"]["finalProfiles"] = ["development"]
            project["impactProfiles"]["profiles"]["development"][1]["paths"] = ["**"]
            selection = resolve_impact_selection(
                root, ROOT, project, self.state(base)
            )
            self.assertEqual(1, selection["schemaVersion"])
            self.assertEqual(["development"], selection["assuranceProfiles"])
            self.assertTrue(selection["policyDigest"].startswith("sha256:"))
            self.assertEqual(["global-tests"], [item["id"] for item in selection["selectedUnits"]])

    def test_current_final_selection_remains_unresolved_without_global_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.make_repository(root)
            (root / "unknown.txt").write_text("unknown\n", encoding="utf-8")
            project = self.project()
            project["impactProfiles"]["finalProfiles"] = ["development"]
            selection = resolve_impact_selection(
                root, ROOT, project, self.state(base)
            )
            self.assertEqual("unresolved", selection["status"])
            self.assertEqual(["development"], selection["assuranceProfiles"])
            self.assertEqual([], selection["selectedUnits"])

    def test_unmapped_path_blocks_without_full_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.make_repository(root)
            (root / "unknown.txt").write_text("unknown\n", encoding="utf-8")
            selection = resolve_impact_selection(
                root,
                ROOT,
                self.project(),
                self.state(base),
            )
            self.assertEqual("unresolved", selection["status"])
            self.assertEqual(
                [{"profile": "development", "path": "unknown.txt"}],
                selection["unresolvedPaths"],
            )
            self.assertEqual(
                "inspect-diff-and-update-impact-policy",
                selection["resolution"]["action"],
            )
            self.assertEqual([], selection["selectedUnits"])

    def test_missing_policy_is_unavailable_not_full(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.make_repository(root)
            (root / "src" / "app.py").write_text("two\n", encoding="utf-8")
            project = self.project()
            project.pop("impactProfiles")
            selection = resolve_impact_selection(root, ROOT, project, self.state(base))
            self.assertEqual("unavailable", selection["status"])
            self.assertEqual([], selection["selectedUnits"])
            self.assertIn("not declared", selection["resolution"]["reason"])

    def test_partial_global_unit_does_not_hide_unmapped_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.make_repository(root)
            (root / "config").mkdir()
            (root / "config" / "policy.json").write_text("policy\n", encoding="utf-8")
            (root / "unknown.txt").write_text("unknown\n", encoding="utf-8")
            selection = resolve_impact_selection(root, ROOT, self.project(), self.state(base))
            self.assertEqual("unresolved", selection["status"])
            self.assertEqual(["global-tests"], [item["id"] for item in selection["selectedUnits"]])
            self.assertEqual(
                [{"profile": "development", "path": "unknown.txt"}],
                selection["unresolvedPaths"],
            )

    def test_lookup_rejects_policy_mutation(self) -> None:
        with self.assertRaisesRegex(ProcessError, "changed while resolving"):
            impact_unit_lookup(
                {"impactProfiles": {"schemaVersion": 1, "profiles": {"development": []}}},
                {
                    "selectedUnits": [
                        {"profile": "development", "id": "app-tests", "matchedPaths": ["src/app.py"]}
                    ]
                },
            )

    def test_invalid_policy_returns_actionable_unavailable_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.make_repository(root)
            (root / "src" / "app.py").write_text("two\n", encoding="utf-8")
            state = self.lifecycle_state(base)
            with patch("engineering_process.lifecycle._load_state", return_value=state), patch(
                "engineering_process.lifecycle.load_project",
                side_effect=ProcessError("impactProfiles.schemaVersion must be 1"),
            ):
                selection = resolve_impact_work(root, ROOT, {}, "sample-change")
            self.assertEqual("unavailable", selection["status"])
            self.assertEqual(
                "inspect-diff-and-update-impact-policy",
                selection["resolution"]["action"],
            )
            self.assertIn("schemaVersion", selection["resolution"]["reason"])

    def test_affected_execution_is_ordered_fail_fast_and_non_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.make_repository(root)
            state = self.lifecycle_state(base)
            project = {"lifecycle": {"requiredProfiles": ["development"]}}
            selection = {
                "status": "ready",
                "changeId": "sample-change",
                "changedPaths": ["src/app.py"],
                "selectedUnits": [
                    {"profile": "development", "id": "first", "matchedPaths": ["src/app.py"]},
                    {"profile": "development", "id": "second", "matchedPaths": ["src/app.py"]},
                ],
            }
            units = {
                ("development", "first"): {"id": "first", "run": ["python"], "timeoutSeconds": 1},
                ("development", "second"): {"id": "second", "run": ["python"], "timeoutSeconds": 1},
            }
            checkpoints = {"head": base, "fingerprint": "sha256:" + "1" * 64, "fileCount": 1, "byteCount": 1}
            calls: list[str] = []

            def run(_root: Path, check: dict) -> dict:
                calls.append(check["id"])
                return {"id": check["id"], "status": "passed", "durationMs": 5}

            with patch("engineering_process.lifecycle._load_state", return_value=state), patch(
                "engineering_process.lifecycle.load_project", return_value=project
            ), patch("engineering_process.lifecycle.verification_lock", return_value=nullcontext()), patch(
                "engineering_process.lifecycle._require_current_baseline"
            ), patch("engineering_process.lifecycle.resolve_impact_selection", return_value=selection), patch(
                "engineering_process.lifecycle.impact_unit_lookup", return_value=units
            ), patch("engineering_process.lifecycle.repository_snapshot", side_effect=[checkpoints, checkpoints]), patch(
                "engineering_process.lifecycle.run_check", side_effect=run
            ), patch("engineering_process.lifecycle._record_impact_event", return_value=state) as record:
                result_state, result_selection, executions = verify_affected(
                    root, ROOT, {}, "sample-change", profiles=("development",)
                )
            self.assertIs(result_state, state)
            self.assertIs(result_selection, selection)
            self.assertEqual(["first", "second"], calls)
            self.assertEqual("passed", executions[0]["status"])
            self.assertEqual(2, executions[0]["launchCount"])
            self.assertEqual(10, executions[0]["scriptDurationMs"])
            self.assertEqual("implementing", result_state["phase"])
            self.assertEqual("impact-verified", record.call_args.args[3])

    def test_affected_execution_stops_on_failure_and_detects_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.make_repository(root)
            state = self.lifecycle_state(base)
            project = {"lifecycle": {"requiredProfiles": ["development"]}}
            selection = {
                "status": "ready",
                "changeId": "sample-change",
                "changedPaths": ["src/app.py"],
                "selectedUnits": [
                    {"profile": "development", "id": "first", "matchedPaths": ["src/app.py"]},
                    {"profile": "development", "id": "second", "matchedPaths": ["src/app.py"]},
                ],
            }
            units = {
                ("development", "first"): {"id": "first", "run": ["python"], "timeoutSeconds": 1},
                ("development", "second"): {"id": "second", "run": ["python"], "timeoutSeconds": 1},
            }
            before = {"head": base, "fingerprint": "sha256:" + "1" * 64, "fileCount": 1, "byteCount": 1}
            after = {"head": base, "fingerprint": "sha256:" + "2" * 64, "fileCount": 1, "byteCount": 1}
            calls: list[str] = []

            def fail(_root: Path, check: dict) -> dict:
                calls.append(check["id"])
                return {"id": check["id"], "status": "failed", "durationMs": 7}

            with patch("engineering_process.lifecycle._load_state", return_value=state), patch(
                "engineering_process.lifecycle.load_project", return_value=project
            ), patch("engineering_process.lifecycle.verification_lock", return_value=nullcontext()), patch(
                "engineering_process.lifecycle._require_current_baseline"
            ), patch("engineering_process.lifecycle.resolve_impact_selection", return_value=selection), patch(
                "engineering_process.lifecycle.impact_unit_lookup", return_value=units
            ), patch("engineering_process.lifecycle.repository_snapshot", side_effect=[before, after]), patch(
                "engineering_process.lifecycle.run_check", side_effect=fail
            ), patch("engineering_process.lifecycle._record_impact_event", return_value=state):
                _state, _selection, executions = verify_affected(
                    root, ROOT, {}, "sample-change", profiles=("development",)
                )
            self.assertEqual(["first"], calls)
            self.assertEqual("failed", executions[0]["status"])
            self.assertEqual("repository-immutability", executions[0]["units"][-1]["id"])

    def test_affected_selection_detects_mutation_before_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.make_repository(root)
            state = self.lifecycle_state(base)
            project = {"lifecycle": {"requiredProfiles": ["development"]}}
            selection = {
                "status": "ready",
                "changeId": "sample-change",
                "changedPaths": ["src/app.py"],
                "selectedUnits": [
                    {
                        "profile": "development",
                        "id": "first",
                        "matchedPaths": ["src/app.py"],
                    }
                ],
            }
            units = {
                ("development", "first"): {
                    "id": "first",
                    "run": ["python"],
                    "timeoutSeconds": 1,
                }
            }
            before = {
                "head": base,
                "fingerprint": "sha256:" + "1" * 64,
                "fileCount": 1,
                "byteCount": 1,
            }
            after = {
                "head": base,
                "fingerprint": "sha256:" + "2" * 64,
                "fileCount": 2,
                "byteCount": 2,
            }

            def select(*_args, **_kwargs):
                (root / "race.txt").write_text("added during selection\n", encoding="utf-8")
                return selection

            def snapshot(_root: Path) -> dict:
                return after if (root / "race.txt").exists() else before

            with patch("engineering_process.lifecycle._load_state", return_value=state), patch(
                "engineering_process.lifecycle.load_project", return_value=project
            ), patch("engineering_process.lifecycle.verification_lock", return_value=nullcontext()), patch(
                "engineering_process.lifecycle._require_current_baseline"
            ), patch(
                "engineering_process.lifecycle.resolve_impact_selection",
                side_effect=select,
            ), patch(
                "engineering_process.lifecycle.impact_unit_lookup", return_value=units
            ), patch(
                "engineering_process.lifecycle.repository_snapshot",
                side_effect=snapshot,
            ), patch(
                "engineering_process.lifecycle.run_check",
                return_value={"id": "first", "status": "passed", "durationMs": 5},
            ), patch(
                "engineering_process.lifecycle._record_impact_event",
                return_value=state,
            ):
                _state, _selection, executions = verify_affected(
                    root, ROOT, {}, "sample-change", profiles=("development",)
                )

            self.assertEqual("failed", executions[0]["status"])
            self.assertEqual("repository-immutability", executions[0]["units"][-1]["id"])


if __name__ == "__main__":
    unittest.main()
