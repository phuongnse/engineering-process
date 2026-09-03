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
from engineering_process.contracts import ProcessError, read_json


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
        legacy_project = {
            "schemaVersion": 3,
            "project": "consumer",
            "lifecycle": {"requiredProfiles": ["development"]},
            "profiles": {
                "development": [
                    {
                        "id": "unit",
                        "run": ["python", "-m", "unittest"],
                        "timeoutSeconds": 300,
                        "components": ["legacy-field-is-dropped"],
                    }
                ]
            },
            "environment": {
                "defaultProfile": "development",
                "foregroundOnly": True,
                "profiles": {
                    "development": ["python-runtime"],
                    "review": ["python-runtime"],
                },
                "requirements": [
                    {
                        "id": "python-runtime",
                        "description": "Python is available",
                        "probe": {
                            "run": ["python", "--version"],
                            "timeoutSeconds": 30,
                            "readOnly": True,
                        },
                        "remediation": "Install Python",
                    }
                ],
                "managedTools": [],
                "setupActions": [
                    {
                        "id": "install-legacy-tool",
                        "kind": "managed-tool",
                        "tool": "legacy-tool",
                        "timeoutSeconds": 30,
                    },
                    {
                        "id": "prepare-native-tool",
                        "kind": "command",
                        "run": ["python", "-c", "raise SystemExit(0)"],
                        "timeoutSeconds": 30,
                        "mutations": ["project-files"],
                    }
                ]
            },
        }
        write_json(self.root / ".process" / "project.json", legacy_project)
        write_json(
            self.root / ".process" / "process.lock",
            {
                "schemaVersion": 1,
                "process": {"version": "0.4.0", "digest": "sha256:" + "0" * 64},
                "skills": ["old-skill", "run-change"],
            },
        )
        old_skill = self.root / ".agents" / "skills" / "old-skill"
        old_skill.mkdir(parents=True)
        (old_skill / "SKILL.md").write_text("old\n", encoding="utf-8")
        (old_skill / "consumer-notes.md").write_text(
            "consumer owned\n", encoding="utf-8"
        )
        old_run = self.root / ".agents" / "skills" / "run-change"
        old_run.mkdir(parents=True)
        (old_run / "SKILL.md").write_text("legacy\n", encoding="utf-8")
        (old_run / "obsolete.txt").write_text("remove\n", encoding="utf-8")
        references = old_run / "references"
        references.mkdir()
        (references / "execution.md").write_text("managed legacy reference\n", encoding="utf-8")
        custom = self.root / ".agents" / "skills" / "consumer-owned"
        custom.mkdir(parents=True)
        (custom / "SKILL.md").write_text("keep\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text(
            "# Consumer rules\n\n<!-- engineering-process:start -->\nold\n<!-- engineering-process:end -->\n",
            encoding="utf-8",
        )
        (self.root / ".process" / "adopt-process.py").write_text("old runner\n", encoding="utf-8")
        (self.root / ".process" / "adopt-process-windows-job.py").write_text("old helper\n", encoding="utf-8")
        (self.root / ".process" / "automation.json").write_text("{}\n", encoding="utf-8")
        migration = self.root / ".process" / "adoption-migrations" / "0.7.0.json"
        migration.parent.mkdir(parents=True)
        migration.write_text("{}\n", encoding="utf-8")
        self.requirements = self.root / "requirements" / "process.txt"
        self.requirements.parent.mkdir()
        self.requirements.write_text(
            f"engineering-process=={VERSION} \\\n+    --hash=sha256:{'a' * 64}\n"
            "jsonschema==4.26.0 \\\n+    --hash=sha256:" + "b" * 64 + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_legacy_consumer_converges_and_second_apply_is_noop(self) -> None:
        first = apply_adoption(
            self.root, PROCESS_ROOT, self.requirements, requirements_source=self.requirements
        )
        self.assertEqual("applied", first["status"])
        self.assertEqual(
            "consumer owned\n",
            (
                self.root
                / ".agents"
                / "skills"
                / "old-skill"
                / "consumer-notes.md"
            ).read_text(encoding="utf-8"),
        )
        self.assertFalse(
            (self.root / ".agents" / "skills" / "old-skill" / "SKILL.md").exists()
        )
        self.assertEqual(
            (PROCESS_ROOT / "templates" / "adopt-process-windows-job.py").read_bytes(),
            (self.root / ".process" / "adopt-process-windows-job.py").read_bytes(),
        )
        self.assertFalse((self.root / ".process" / "automation.json").exists())
        self.assertFalse((self.root / ".process" / "adoption-migrations").exists())
        self.assertTrue((self.root / ".agents" / "skills" / "consumer-owned" / "SKILL.md").is_file())
        self.assertTrue((self.root / ".agents" / "skills" / "improve-process" / "SKILL.md").is_file())
        self.assertEqual(
            "remove\n",
            (self.root / ".agents" / "skills" / "run-change" / "obsolete.txt").read_text(
                encoding="utf-8"
            ),
        )
        self.assertFalse(
            (self.root / ".agents" / "skills" / "run-change" / "references" / "execution.md").exists()
        )
        adopted_template = (
            self.root / ".github" / "PULL_REQUEST_TEMPLATE.md"
        ).read_text(encoding="utf-8")
        source_template = (
            PROCESS_ROOT / "templates" / "PULL_REQUEST_TEMPLATE.md"
        ).read_text(encoding="utf-8")
        self.assertIn(source_template, adopted_template)
        self.assertIn("- Completion receipt:", adopted_template)
        self.assertNotIn("Record the independent reviewer", adopted_template)
        self.assertIn("# Consumer rules", (self.root / "AGENTS.md").read_text(encoding="utf-8"))
        project = read_json(self.root / ".process" / "project.json")
        self.assertEqual(5, project["schemaVersion"])
        self.assertEqual(1, len(project["setup"]))
        self.assertEqual("prepare-native-tool", project["setup"][0]["id"])
        lock = read_json(self.root / ".process" / "process.lock")
        self.assertEqual(2, lock["schemaVersion"])
        self.assertEqual(VERSION, lock["process"]["version"])
        self.assertIn(".agents/skills/run-change/SKILL.md", lock["managedFiles"])
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
        collision = self.root / ".agents" / "skills" / "improve-process" / "SKILL.md"
        collision.parent.mkdir(parents=True)
        collision.write_text("consumer skill\n", encoding="utf-8")
        with self.assertRaisesRegex(ProcessError, "consumer-owned path collides"):
            apply_adoption(self.root, PROCESS_ROOT, self.requirements)
        self.assertEqual("consumer skill\n", collision.read_text(encoding="utf-8"))

    def test_v2_lock_cannot_claim_a_consumer_owned_path(self) -> None:
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

        with self.assertRaisesRegex(ProcessError, "managedFiles"):
            apply_adoption(self.root, PROCESS_ROOT, self.requirements)

        self.assertEqual(
            "consumer documentation\n", readme.read_text(encoding="utf-8")
        )

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
        self.assertEqual("old\n", (self.root / ".agents" / "skills" / "old-skill" / "SKILL.md").read_text(encoding="utf-8"))

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
