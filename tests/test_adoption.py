from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock
import sys

from engineering_process import VERSION
from engineering_process.adoption import apply_adoption, check_adoption
from engineering_process.artifact_standards import resolve_standard
from engineering_process.contracts import ProcessError, read_json
from engineering_process.repository import repository_snapshot


PROCESS_ROOT = Path(__file__).resolve().parent.parent
def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def load_managed_adopter(name: str) -> object:
    path = PROCESS_ROOT / "templates" / "adopt-process.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class AdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        current_project = {
            "schemaVersion": 1,
            "project": "consumer",
            "lifecycle": {"requiredProfiles": ["development"]},
            "profiles": {
                "development": [
                    {
                        "id": "unit",
                        "run": ["python", "-m", "unittest"],
                        "timeoutSeconds": 300,
                    }
                ]
            },
            "setup": [
                {
                    "id": "prepare-native-tool",
                    "run": ["python", "-c", "raise SystemExit(0)"],
                    "timeoutSeconds": 30,
                }
            ],
        }
        write_json(self.root / ".process" / "project.json", current_project)
        custom = self.root / ".agents" / "skills" / "consumer-owned"
        custom.mkdir(parents=True)
        (custom / "SKILL.md").write_text("keep\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text(
            "# Consumer rules\n\n<!-- engineering-process:start -->\nold\n<!-- engineering-process:end -->\n",
            encoding="utf-8",
        )
        self.requirements = self.root / "requirements" / "process.txt"
        self.requirements.parent.mkdir()
        self.requirements.write_text(
            f"engineering-process=={VERSION} \\\n+    --hash=sha256:{'a' * 64}\n"
            "jsonschema==4.26.0 \\\n+    --hash=sha256:" + "b" * 64 + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_adoption_preserves_consumer_standard_and_generates_its_template(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True, timeout=30)
        document = resolve_standard(None, PROCESS_ROOT, "pull-request").document
        document["id"] = "consumer.pr"
        document["rules"]["sections"][0]["heading"] = "## Consumer changes"
        definition = self.root / ".process" / "consumer-pr.json"
        selector = self.root / ".process" / "standards.json"
        write_json(definition, document)
        issue = resolve_standard(None, PROCESS_ROOT, "issue").document
        issue["id"] = "consumer.issue"
        issue["rules"]["title"]["prefix"] = "[consumer] "
        issue_definition = self.root / ".process" / "consumer-issue.json"
        write_json(issue_definition, issue)
        write_json(selector, {"schemaVersion": 1, "artifacts": {
            "pull-request": {"path": ".process/consumer-pr.json"},
            "issue": {"path": ".process/consumer-issue.json"},
        }})
        owned = {path: path.read_bytes() for path in (definition, issue_definition, selector)}
        self.assertEqual("applied", apply_adoption(self.root, PROCESS_ROOT, self.requirements)["status"])
        self.assertIn("## Consumer changes", (self.root / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8"))
        for path, content in owned.items():
            self.assertEqual(content, path.read_bytes())
        lock = read_json(self.root / ".process" / "process.lock")
        self.assertNotIn(".process/consumer-pr.json", lock["managedFiles"])
        self.assertNotIn(".process/consumer-issue.json", lock["managedFiles"])
        self.assertNotIn(".process/standards.json", lock["managedFiles"])
        self.assertEqual("passed", check_adoption(self.root, PROCESS_ROOT, self.requirements)["status"])
        self.assertEqual("unchanged", apply_adoption(self.root, PROCESS_ROOT, self.requirements)["status"])

    def test_selected_standard_collisions_fail_before_any_adoption_mutation(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True, timeout=30)
        self.assertEqual("applied", apply_adoption(self.root, PROCESS_ROOT, self.requirements)["status"])
        selector = self.root / ".process" / "standards.json"
        paths = (
            ".agents/skills/change-plan/.engineering-process.json",
        )
        for artifact in ("pull-request", "release-notes"):
            document = resolve_standard(None, PROCESS_ROOT, artifact).document
            for relative in paths:
                with self.subTest(artifact=artifact, path=relative):
                    definition = self.root / relative
                    original = definition.read_bytes() if definition.exists() else None
                    write_json(definition, document)
                    write_json(selector, {"schemaVersion": 1, "artifacts": {artifact: {"path": relative}}})
                    self.assertEqual(document["id"], resolve_standard(self.root, PROCESS_ROOT, artifact).document["id"])
                    try:
                        before = repository_snapshot(self.root)
                        with self.assertRaisesRegex(ProcessError, "conflict with managed adoption paths"):
                            apply_adoption(self.root, PROCESS_ROOT, self.requirements)
                        self.assertEqual(before, repository_snapshot(self.root))
                    finally:
                        if original is None:
                            definition.unlink()
                        else:
                            definition.write_bytes(original)
        # Relocating the release-only override restores ordinary adoption and replay.
        definition = self.root / ".process" / "consumer-release.json"
        write_json(definition, document)
        write_json(selector, {"schemaVersion": 1, "artifacts": {"release-notes": {"path": ".process/consumer-release.json"}}})
        expected = definition.read_bytes()
        self.assertIn(apply_adoption(self.root, PROCESS_ROOT, self.requirements)["status"], {"applied", "unchanged"})
        self.assertEqual(expected, definition.read_bytes())
        self.assertEqual("passed", check_adoption(self.root, PROCESS_ROOT, self.requirements)["status"])
        self.assertEqual("unchanged", apply_adoption(self.root, PROCESS_ROOT, self.requirements)["status"])

    def test_current_consumer_converges_and_second_apply_is_noop(self) -> None:
        retained = (
            self.root / ".process" / "adoption-migrations" / "unowned.json",
            self.root / ".process" / "automation.json",
        )
        for path in retained:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("consumer-owned\n", encoding="utf-8")
        first = apply_adoption(
            self.root, PROCESS_ROOT, self.requirements, requirements_source=self.requirements
        )
        self.assertEqual("applied", first["status"])
        self.assertEqual(
            (PROCESS_ROOT / "templates" / "adopt-process-windows-job.py").read_bytes(),
            (self.root / ".process" / "adopt-process-windows-job.py").read_bytes(),
        )
        self.assertTrue((self.root / ".agents" / "skills" / "consumer-owned" / "SKILL.md").is_file())
        self.assertTrue((self.root / ".agents" / "skills" / "process-improve" / "SKILL.md").is_file())
        for path in retained:
            self.assertEqual("consumer-owned\n", path.read_text(encoding="utf-8"))
        self.assertEqual(
            (
                PROCESS_ROOT
                / "process_assets"
                / "skills"
                / "production-engineering"
                / "invariants.json"
            ).read_bytes(),
            (
                self.root
                / ".agents"
                / "skills"
                / "production-engineering"
                / "invariants.json"
            ).read_bytes(),
        )
        adopted_template = (
            self.root / ".github" / "PULL_REQUEST_TEMPLATE.md"
        ).read_text(encoding="utf-8")
        source_template = (
            PROCESS_ROOT / "templates" / "PULL_REQUEST_TEMPLATE.md"
        ).read_text(encoding="utf-8")
        self.assertIn(source_template, adopted_template)
        self.assertIn("- Completion receipt:", adopted_template)
        self.assertIn("## Review and completion", adopted_template)
        self.assertNotIn("Record the independent reviewer", adopted_template)
        self.assertIn("# Consumer rules", (self.root / "AGENTS.md").read_text(encoding="utf-8"))
        project = read_json(self.root / ".process" / "project.json")
        self.assertEqual(1, project["schemaVersion"])
        self.assertEqual(1, len(project["setup"]))
        self.assertEqual("prepare-native-tool", project["setup"][0]["id"])
        lock = read_json(self.root / ".process" / "process.lock")
        self.assertEqual(1, lock["schemaVersion"])
        self.assertEqual(VERSION, lock["process"]["version"])
        self.assertIn(".agents/skills/deliver-change/SKILL.md", lock["managedFiles"])
        self.assertIn(".process/adopt-process-windows-job.py", lock["managedFiles"])

        second = apply_adoption(self.root, PROCESS_ROOT, self.requirements)
        self.assertEqual("unchanged", second["status"])
        self.assertEqual("passed", check_adoption(self.root, PROCESS_ROOT, self.requirements)["status"])

    def test_wrong_or_unhashed_pin_is_rejected(self) -> None:
        self.requirements.write_text("engineering-process==99.0.0\n", encoding="utf-8")
        with self.assertRaisesRegex(ProcessError, "pins 99.0.0"):
            apply_adoption(self.root, PROCESS_ROOT, self.requirements)
        self.requirements.write_text(f"engineering-process=={VERSION}\n", encoding="utf-8")
        with self.assertRaisesRegex(ProcessError, "not hash locked"):
            apply_adoption(self.root, PROCESS_ROOT, self.requirements)

    def test_consumer_owned_skill_name_collision_fails_closed(self) -> None:
        collision = self.root / ".agents" / "skills" / "process-improve" / "SKILL.md"
        collision.parent.mkdir(parents=True)
        collision.write_text("consumer skill\n", encoding="utf-8")
        with self.assertRaisesRegex(ProcessError, "consumer-owned path collides"):
            apply_adoption(self.root, PROCESS_ROOT, self.requirements)
        self.assertEqual("consumer skill\n", collision.read_text(encoding="utf-8"))

    def test_non_current_lock_is_rejected_without_mutation(self) -> None:
        readme = self.root / "README.md"
        readme.write_text("consumer documentation\n", encoding="utf-8")
        write_json(
            self.root / ".process" / "process.lock",
            {
                "schemaVersion": 2,
                "process": {
                    "package": "engineering-process",
                    "version": "0.4.0",
                    "digest": "sha256:" + "0" * 64,
                },
                "requirementsDigest": "sha256:" + "1" * 64,
                "skills": ["old-skill", "run-change"],
                "managedFiles": ["README.md"],
            },
        )

        with self.assertRaisesRegex(ProcessError, "schemaVersion"):
            apply_adoption(self.root, PROCESS_ROOT, self.requirements)

        self.assertEqual(
            "consumer documentation\n", readme.read_text(encoding="utf-8")
        )

    def test_older_lock_is_migrated_and_obsolete_managed_files_are_removed(self) -> None:
        self.assertEqual("applied", apply_adoption(self.root, PROCESS_ROOT, self.requirements)["status"])
        obsolete = self.root / ".agents" / "skills" / "process-improve" / "obsolete.md"
        obsolete.write_text("old managed file\n", encoding="utf-8")
        lock_path = self.root / ".process" / "process.lock"
        lock = read_json(lock_path)
        lock["process"]["version"] = "3.2.0"
        lock["process"]["digest"] = "sha256:" + "2" * 64
        lock["managedFiles"].append(".agents/skills/process-improve/obsolete.md")
        write_json(lock_path, lock)

        result = apply_adoption(self.root, PROCESS_ROOT, self.requirements)

        self.assertEqual("applied", result["status"])
        self.assertFalse(obsolete.exists())
        migrated = read_json(lock_path)
        self.assertEqual(VERSION, migrated["process"]["version"])
        self.assertNotIn(".agents/skills/process-improve/obsolete.md", migrated["managedFiles"])
        self.assertEqual("passed", check_adoption(self.root, PROCESS_ROOT, self.requirements)["status"])

    def test_newer_lock_is_rejected_without_mutation(self) -> None:
        lock_path = self.root / ".process" / "process.lock"
        lock = {
            "schemaVersion": 1,
            "process": {
                "package": "engineering-process",
                "version": "99.0.0",
                "digest": "sha256:" + "1" * 64,
            },
            "requirementsDigest": "sha256:" + "1" * 64,
            "skills": ["process-improve"],
            "managedFiles": [".agents/skills/process-improve/SKILL.md"],
        }
        write_json(lock_path, lock)
        before = lock_path.read_bytes()
        with self.assertRaisesRegex(ProcessError, "newer package"):
            apply_adoption(self.root, PROCESS_ROOT, self.requirements)
        self.assertEqual(before, lock_path.read_bytes())

    def test_current_lock_with_mismatched_distribution_is_rejected_without_mutation(self) -> None:
        lock_path = self.root / ".process" / "process.lock"
        lock = {
            "schemaVersion": 1,
            "process": {
                "package": "engineering-process",
                "version": VERSION,
                "digest": "sha256:" + "0" * 64,
            },
            "requirementsDigest": "sha256:" + "1" * 64,
            "skills": ["process-improve"],
            "managedFiles": [".agents/skills/process-improve/SKILL.md"],
        }
        write_json(lock_path, lock)
        before = lock_path.read_bytes()
        with self.assertRaisesRegex(ProcessError, "current package and distribution"):
            apply_adoption(self.root, PROCESS_ROOT, self.requirements)
        self.assertEqual(before, lock_path.read_bytes())

    def test_private_snapshot_must_match_checkout_lock(self) -> None:
        snapshot = self.root / "requirements" / "snapshot.txt"
        snapshot.write_text(self.requirements.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
        with self.assertRaisesRegex(ProcessError, "differ"):
            apply_adoption(
                self.root,
                PROCESS_ROOT,
                snapshot,
                requirements_source=self.requirements,
            )

    def test_write_failure_restores_all_original_files(self) -> None:
        apply_adoption(self.root, PROCESS_ROOT, self.requirements)
        agents = self.root / "AGENTS.md"
        agents.write_text(
            agents.read_text(encoding="utf-8").replace("deliver-change", "invalid-process-skill", 1),
            encoding="utf-8",
        )
        original_files = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        original_lock = (self.root / ".process" / "process.lock").read_bytes()
        original_replace = os.replace
        calls = 0

        def fail_second(source: object, target: object, *args: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected write failure")
            original_replace(source, target, *args, **kwargs)

        with mock.patch("engineering_process.adoption.os.replace", fail_second):
            with self.assertRaisesRegex(ProcessError, "rolled back"):
                apply_adoption(self.root, PROCESS_ROOT, self.requirements)
        self.assertEqual(original_lock, (self.root / ".process" / "process.lock").read_bytes())
        self.assertEqual(
            original_files,
            {
                path.relative_to(self.root): path.read_bytes()
                for path in self.root.rglob("*")
                if path.is_file()
            },
        )

    def test_predictable_temporary_symlink_cannot_escape_checkout(self) -> None:
        outside = Path(self.temporary.name).parent / f"outside-{id(self)}.txt"
        outside.write_text("outside\n", encoding="utf-8")
        trap = self.root / ".AGENTS.md.adoption.tmp"
        trap.symlink_to(outside)
        try:
            apply_adoption(self.root, PROCESS_ROOT, self.requirements)
            self.assertEqual("outside\n", outside.read_text(encoding="utf-8"))
            self.assertFalse((self.root / "AGENTS.md").is_symlink())
        finally:
            outside.unlink(missing_ok=True)

    def test_post_write_guard_failure_rolls_back_managed_state(self) -> None:
        apply_adoption(self.root, PROCESS_ROOT, self.requirements)
        agents = self.root / "AGENTS.md"
        agents.write_text(
            agents.read_text(encoding="utf-8").replace("deliver-change", "invalid-process-skill", 1),
            encoding="utf-8",
        )
        original_lock = (self.root / ".process" / "process.lock").read_bytes()
        original_replace = os.replace
        changed = False

        def change_requirements_after_first_write(
            source: object, target: object, *args: object, **kwargs: object
        ) -> None:
            nonlocal changed
            original_replace(source, target, *args, **kwargs)
            if not changed:
                changed = True
                self.requirements.write_bytes(self.requirements.read_bytes() + b"# raced\n")

        with mock.patch(
            "engineering_process.adoption.os.replace",
            change_requirements_after_first_write,
        ):
            with self.assertRaisesRegex(ProcessError, "rolled back"):
                apply_adoption(
                    self.root,
                    PROCESS_ROOT,
                    self.requirements,
                    requirements_source=self.requirements,
                )
        self.assertEqual(
            original_lock,
            (self.root / ".process" / "process.lock").read_bytes(),
        )

    def test_managed_runner_enforces_aggregate_output_limit(self) -> None:
        module = load_managed_adopter("managed_adopter")
        with self.assertRaisesRegex(RuntimeError, "output exceeded"):
            module._run(
                [sys.executable, "-c", "print('x' * 2000000)"],
                cwd=self.root,
            )

    def test_managed_runner_stops_immediately_when_output_limit_is_exceeded(self) -> None:
        module = load_managed_adopter("managed_adopter_early_output")
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "output exceeded"):
            module._run(
                [
                    sys.executable,
                    "-c",
                    "import sys, time; "
                    "sys.stdout.write('x' * 2000000); sys.stdout.flush(); "
                    "time.sleep(5)",
                ],
                cwd=self.root,
            )
        self.assertLess(time.monotonic() - started, 2)

    @unittest.skipUnless(
        sys.platform.startswith("linux"), "Linux subreaper ownership assertion"
    )
    def test_managed_runner_does_not_terminate_an_unrelated_child(self) -> None:
        module = load_managed_adopter("managed_adopter_owned_children")
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            module._run(
                [sys.executable, "-c", "raise SystemExit(0)"], cwd=self.root
            )
            self.assertIsNone(unrelated.poll())
        finally:
            if unrelated.poll() is None:
                unrelated.terminate()
            unrelated.wait(timeout=3)

    def test_managed_runner_does_not_surface_raw_stderr(self) -> None:
        module = load_managed_adopter("managed_adopter_secret")
        with self.assertRaises(RuntimeError) as caught:
            module._run(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('TOPSECRET', file=sys.stderr); raise SystemExit(2)",
                ],
                cwd=self.root,
            )
        self.assertNotIn("TOPSECRET", str(caught.exception))
        self.assertIn("stderrSha256", str(caught.exception))

    @unittest.skipUnless(
        sys.platform.startswith("linux"), "Linux subreaper containment assertion"
    )
    def test_managed_runner_terminates_detached_descendants(self) -> None:
        module = load_managed_adopter("managed_adopter_detached")
        pid_path = self.root / "detached.pid"
        child = "import time; time.sleep(30)"
        script = (
            "import pathlib, subprocess, sys; "
            f"p=subprocess.Popen([sys.executable, '-c', {child!r}], "
            "start_new_session=True, stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL); "
            "pathlib.Path(sys.argv[1]).write_text(str(p.pid), encoding='utf-8')"
        )
        with self.assertRaisesRegex(RuntimeError, "descendant"):
            module._run(
                [sys.executable, "-c", script, str(pid_path)],
                cwd=self.root,
            )
        pid = int(pid_path.read_text(encoding="utf-8"))
        self.assertFalse(Path(f"/proc/{pid}").exists())

    @unittest.skipUnless(os.name == "nt", "Windows Job Object containment assertion")
    def test_managed_runner_uses_windows_job_object(self) -> None:
        module = load_managed_adopter("managed_adopter_windows")
        pid_path = self.root / "windows-child.pid"
        script = (
            "import pathlib, subprocess, sys; "
            "p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
            "pathlib.Path(sys.argv[1]).write_text(str(p.pid), encoding='utf-8')"
        )
        with self.assertRaisesRegex(RuntimeError, "descendant"):
            module._run(
                [sys.executable, "-c", script, str(pid_path)],
                cwd=self.root,
            )


if __name__ == "__main__":
    unittest.main()
