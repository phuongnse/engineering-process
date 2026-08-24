import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from engineering_process import syncing
from engineering_process import VERSION
from engineering_process.contracts import (
    ContractError,
    ProcessLock,
    read_json,
    validate_process_lock,
)
from engineering_process.distribution import distribution_digest
from engineering_process.git_attributes import (
    ATTRIBUTES_END,
    ATTRIBUTES_INPUT_LIMIT,
    ATTRIBUTES_START,
)
from engineering_process.syncing import (
    git_attributes_target_issues,
    sync_skills,
    synchronized_state,
)


PROCESS_ROOT = Path(__file__).resolve().parent.parent
CORE_SKILLS = (
    "define-change-contract",
    "evolve-process",
    "finish-change",
    "implement-change",
    "plan-change",
    "review-change",
    "run-change",
    "verify-change",
)


class SyncTests(unittest.TestCase):
    def test_windows_file_identity_uses_nonzero_file_id_not_incomplete_device(self):
        path_stat = SimpleNamespace(st_dev=0, st_ino=123)
        handle_stat = SimpleNamespace(st_dev=456, st_ino=123)

        with mock.patch.object(syncing.os, "name", "nt"):
            self.assertTrue(syncing._same_file_identity(path_stat, handle_stat))
            self.assertFalse(
                syncing._same_file_identity(
                    path_stat, SimpleNamespace(st_dev=456, st_ino=124)
                )
            )
            self.assertFalse(
                syncing._same_file_identity(
                    SimpleNamespace(st_dev=123, st_ino=123),
                    SimpleNamespace(st_dev=456, st_ino=123),
                )
            )
            self.assertFalse(
                syncing._same_file_identity(
                    SimpleNamespace(st_dev=0, st_ino=0),
                    SimpleNamespace(st_dev=456, st_ino=0),
                )
            )

    def test_managed_skill_comparison_does_not_use_cached_direntry_identity(self):
        original_scandir = os.scandir

        class CachedEntry:
            def __init__(self, entry):
                self.name = entry.name
                self.path = entry.path

            def stat(self, *, follow_symlinks=True):
                value = os.stat(self.path, follow_symlinks=follow_symlinks)
                return SimpleNamespace(
                    st_dev=0,
                    st_ino=0,
                    st_mode=value.st_mode,
                    st_mtime_ns=value.st_mtime_ns,
                    st_size=value.st_size,
                )

        class CachedScan:
            def __init__(self, path):
                with original_scandir(path) as entries:
                    self.entries = [CachedEntry(entry) for entry in entries]

            def __enter__(self):
                return iter(self.entries)

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "file.txt").write_text("stable\n", encoding="utf-8")
            content = (root / "file.txt").read_bytes()
            with mock.patch.object(syncing.os, "scandir", side_effect=CachedScan):
                self.assertEqual(
                    {
                        "file.txt": (
                            len(content),
                            hashlib.sha256(content).hexdigest(),
                        )
                    },
                    syncing._files(root, ignore_marker=False),
                )

    def test_adoption_runner_crlf_marker_remains_managed(self):
        self.assertTrue(
            syncing._has_adoption_runner_marker(
                b"# Managed by engineering-process; do not edit.\r\ncontent\r\n"
            )
        )
        self.assertFalse(
            syncing._has_adoption_runner_marker(
                b"# Similar but unmanaged\r\ncontent\r\n"
            )
        )

    def test_managed_skill_comparison_rejects_symlinks_and_resource_overflow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "file.txt").write_text("bounded\n", encoding="utf-8")
            link = root / "link.txt"
            try:
                link.symlink_to("file.txt")
            except OSError:
                self.skipTest("symlinks are unavailable on this platform")
            with self.assertRaisesRegex(ContractError, "rejects symlinks"):
                syncing._files(root, ignore_marker=False)

            link.unlink()
            (root / "large.bin").write_bytes(b"x" * 11)
            with (
                mock.patch.object(syncing, "MAX_SKILL_FILE_BYTES", 10),
                self.assertRaisesRegex(ContractError, "file exceeds 10 bytes"),
            ):
                syncing._files(root, ignore_marker=False)

            (root / "large.bin").write_bytes(b"123456")
            (root / "second.bin").write_bytes(b"123456")
            with (
                mock.patch.object(syncing, "MAX_SKILL_TOTAL_BYTES", 10),
                self.assertRaisesRegex(ContractError, "content exceeds 10 bytes"),
            ):
                syncing._files(root, ignore_marker=False)
            with (
                mock.patch.object(syncing, "MAX_SKILL_ENTRIES", 1),
                self.assertRaisesRegex(ContractError, "entry count exceeds 1"),
            ):
                syncing._files(root, ignore_marker=False)
            with (
                mock.patch.object(syncing, "SKILL_COMPARISON_TIMEOUT_SECONDS", 0),
                self.assertRaisesRegex(ContractError, "comparison exceeded 0 seconds"),
            ):
                syncing._files(root, ignore_marker=False)

    def test_managed_skill_stable_read_rejects_concurrent_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "file.txt"
            replacement = root / "replacement.txt"
            target.write_text("original\n", encoding="utf-8")
            replacement.write_text("replaced\n", encoding="utf-8")
            real_open = Path.open
            replaced = False

            def replace_then_open(path, *args, **kwargs):
                nonlocal replaced
                if Path(path) == target and not replaced:
                    replaced = True
                    os.replace(replacement, target)
                return real_open(path, *args, **kwargs)

            with (
                mock.patch.object(Path, "open", replace_then_open),
                self.assertRaisesRegex(ContractError, "changed while opening"),
            ):
                syncing._files(root, ignore_marker=False)

    def prepare_project(self, root: Path) -> None:
        process = root / ".process"
        process.mkdir()
        digest = distribution_digest(PROCESS_ROOT, CORE_SKILLS)
        (process / "process.lock").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "process": {"version": VERSION, "digest": digest},
                    "skills": list(CORE_SKILLS),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def test_syncs_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            self.prepare_project(project_root)

            self.assertEqual(
                sync_skills(project_root, PROCESS_ROOT, check=False),
                [],
            )
            lock = validate_process_lock(
                read_json(project_root / ".process" / "process.lock")
            )
            self.assertEqual(
                synchronized_state(project_root, PROCESS_ROOT, lock),
                [],
            )
            attributes = (project_root / ".agents" / ".gitattributes").read_text(
                encoding="utf-8"
            )
            self.assertTrue(attributes.rstrip().endswith(ATTRIBUTES_END))

            target = (
                project_root
                / ".agents"
                / "skills"
                / "verify-change"
                / "SKILL.md"
            )
            target.write_text("tampered\n", encoding="utf-8")

            self.assertTrue(
                any(
                    "content differs" in issue
                    for issue in synchronized_state(project_root, PROCESS_ROOT, lock)
                )
            )

    def test_sync_manages_adoption_runner_and_refuses_unmanaged_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            self.prepare_project(project_root)
            self.assertEqual([], sync_skills(project_root, PROCESS_ROOT, check=False))
            runner = project_root / ".process" / "adopt-process.py"
            self.assertEqual(
                (PROCESS_ROOT / "templates" / "adopt-process.py").read_bytes(),
                runner.read_bytes(),
            )
            windows_helper = (
                project_root / ".process" / "adopt-process-windows-job.py"
            )
            self.assertEqual(
                (
                    PROCESS_ROOT / "templates" / "adopt-process-windows-job.py"
                ).read_bytes(),
                windows_helper.read_bytes(),
            )
            runner.write_text("print('unmanaged')\n", encoding="utf-8")

            with self.assertRaisesRegex(ContractError, "unmanaged adoption runner"):
                sync_skills(project_root, PROCESS_ROOT, check=False)

            runner.write_bytes(
                (PROCESS_ROOT / "templates" / "adopt-process.py").read_bytes()
            )
            windows_helper.write_text("print('unmanaged')\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "unmanaged adoption runner"):
                sync_skills(project_root, PROCESS_ROOT, check=False)

    def test_sync_repairs_managed_attributes_and_preserves_project_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            self.prepare_project(project_root)
            project_attributes = project_root / ".gitattributes"
            project_bytes = b"  # project comment\r\n*.png binary  \r\n\r\n"
            project_attributes.write_bytes(project_bytes)
            self.assertEqual([], sync_skills(project_root, PROCESS_ROOT, check=False))

            attributes = project_root / ".agents" / ".gitattributes"
            current = attributes.read_text(encoding="utf-8")
            attributes.write_text(
                current.replace("text=auto eol=lf", "-text")
                + "skills/** text eol=crlf\n",
                encoding="utf-8",
            )
            lock = validate_process_lock(
                read_json(project_root / ".process" / "process.lock")
            )
            issues = synchronized_state(project_root, PROCESS_ROOT, lock)
            self.assertTrue(any(".gitattributes" in issue for issue in issues))

            self.assertEqual([], sync_skills(project_root, PROCESS_ROOT, check=False))
            repaired = attributes.read_text(encoding="utf-8")
            self.assertIn(
                "skills/** text=auto eol=lf",
                repaired,
            )
            self.assertEqual(1, repaired.count(ATTRIBUTES_START))
            self.assertEqual(1, repaired.count(ATTRIBUTES_END))
            self.assertTrue(repaired.rstrip().endswith(ATTRIBUTES_END))
            self.assertEqual(project_bytes, project_attributes.read_bytes())

    def test_managed_attributes_keep_skill_checkout_bytes_canonical(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            self.prepare_project(project_root)
            (project_root / ".gitattributes").write_text(
                "/.agents/.gitattributes text eol=crlf "
                "working-tree-encoding=UTF-16 filter=project ident\n"
                "/.agents/skills/** text eol=crlf "
                "working-tree-encoding=UTF-16 filter=project ident\n",
                encoding="utf-8",
            )
            self.assertEqual([], sync_skills(project_root, PROCESS_ROOT, check=False))
            attributes = project_root / ".agents" / ".gitattributes"
            target = (
                project_root
                / ".agents"
                / "skills"
                / "verify-change"
                / "SKILL.md"
            )
            relative = target.relative_to(project_root).as_posix()
            attributes_relative = attributes.relative_to(project_root).as_posix()
            subprocess.run(
                ["git", "init", "--quiet"],
                cwd=project_root,
                check=True,
            )
            subprocess.run(
                ["git", "add", ".gitattributes"],
                cwd=project_root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "core.autocrlf", "true"],
                cwd=project_root,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "add",
                    ".agents/.gitattributes",
                    relative,
                ],
                cwd=project_root,
                check=True,
            )
            effective = subprocess.run(
                [
                    "git",
                    "check-attr",
                    "text",
                    "eol",
                    "working-tree-encoding",
                    "filter",
                    "ident",
                    "--",
                    relative,
                ],
                cwd=project_root,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("text: auto", effective.stdout)
            self.assertIn("eol: lf", effective.stdout)
            self.assertIn("working-tree-encoding: unset", effective.stdout)
            self.assertIn("filter: unset", effective.stdout)
            self.assertIn("ident: unset", effective.stdout)
            attributes_effective = subprocess.run(
                [
                    "git",
                    "check-attr",
                    "text",
                    "eol",
                    "working-tree-encoding",
                    "filter",
                    "ident",
                    "--",
                    attributes_relative,
                ],
                cwd=project_root,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("text: auto", attributes_effective.stdout)
            self.assertIn("eol: lf", attributes_effective.stdout)
            self.assertIn(
                "working-tree-encoding: unset", attributes_effective.stdout
            )
            self.assertIn("filter: unset", attributes_effective.stdout)
            self.assertIn("ident: unset", attributes_effective.stdout)
            attributes.write_bytes(attributes.read_bytes().replace(b"\n", b"\r\n"))
            target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))
            self.assertIn(b"\r\n", attributes.read_bytes())
            self.assertIn(b"\r\n", target.read_bytes())

            subprocess.run(
                ["git", "checkout", "--", attributes_relative, relative],
                cwd=project_root,
                check=True,
            )

            self.assertNotIn(b"\r\n", attributes.read_bytes())
            self.assertNotIn(b"\r\n", target.read_bytes())

            (project_root / ".git" / "info" / "attributes").write_text(
                ".agents/skills/** text eol=crlf\n",
                encoding="utf-8",
            )
            target.unlink()
            subprocess.run(
                ["git", "checkout", "--", relative],
                cwd=project_root,
                check=True,
            )
            self.assertIn(b"\r\n", target.read_bytes())
            lock = validate_process_lock(
                read_json(project_root / ".process" / "process.lock")
            )
            self.assertTrue(
                any(
                    "content differs" in issue
                    for issue in synchronized_state(project_root, PROCESS_ROOT, lock)
                )
            )

    @unittest.skipIf(
        os.name == "nt", "symlink creation requires elevated Windows policy"
    )
    def test_sync_preflights_managed_attributes_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            self.prepare_project(project_root)
            attributes = project_root / ".agents" / ".gitattributes"
            attributes.parent.mkdir()
            attributes.symlink_to(project_root / "outside-attributes")

            with self.assertRaisesRegex(ContractError, "must not be a symlink"):
                sync_skills(project_root, PROCESS_ROOT, check=False)

            self.assertFalse((project_root / "AGENTS.md").exists())
            self.assertFalse((project_root / ".github").exists())
            self.assertFalse((project_root / ".agents" / "skills").exists())

    def test_sync_refuses_unmanaged_agent_attributes_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            self.prepare_project(project_root)
            attributes = project_root / ".agents" / ".gitattributes"
            attributes.parent.mkdir()
            attributes.write_text(
                "skills/** text eol=crlf\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ContractError, "refusing to overwrite unmanaged Git attributes"
            ):
                sync_skills(project_root, PROCESS_ROOT, check=False)

            self.assertFalse((project_root / "AGENTS.md").exists())
            self.assertFalse((project_root / ".github").exists())
            self.assertFalse((project_root / ".agents" / "skills").exists())

    def test_managed_attributes_input_is_bounded(self):
        marker = f"{ATTRIBUTES_START}\n".encode()
        for extra in (0, 1):
            with (
                self.subTest(extra=extra),
                tempfile.TemporaryDirectory() as directory,
            ):
                project_root = Path(directory)
                self.prepare_project(project_root)
                attributes = project_root / ".agents" / ".gitattributes"
                attributes.parent.mkdir()
                size = ATTRIBUTES_INPUT_LIMIT + extra
                attributes.write_bytes(marker + b"#" * (size - len(marker)))

                issues = git_attributes_target_issues(project_root)
                if extra == 0:
                    self.assertEqual([], issues)
                    self.assertEqual(
                        [], sync_skills(project_root, PROCESS_ROOT, check=False)
                    )
                else:
                    self.assertTrue(any("exceed" in issue for issue in issues))
                    with self.assertRaisesRegex(ContractError, "exceed"):
                        sync_skills(project_root, PROCESS_ROOT, check=False)
                    self.assertFalse((project_root / "AGENTS.md").exists())
                    self.assertFalse((project_root / ".github").exists())
                    self.assertFalse((project_root / ".agents" / "skills").exists())

    def test_sync_maintains_pr_template_block_and_preserves_extensions(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            self.prepare_project(project_root)
            self.assertEqual([], sync_skills(project_root, PROCESS_ROOT, check=False))
            target = project_root / ".github" / "PULL_REQUEST_TEMPLATE.md"
            canonical = target.read_text(encoding="utf-8")
            target.write_text(
                canonical.replace(
                    "accepted scope is implemented without unapproved expansion",
                    "scope is optional",
                )
                + "\n## Project-specific requirements\n\n"
                + "- [ ] **Project-specific: UI evidence** — record project evidence. "
                + "[status: pending]\n",
                encoding="utf-8",
            )
            lock = validate_process_lock(
                read_json(project_root / ".process" / "process.lock")
            )

            self.assertTrue(
                any(
                    "managed pull-request template differs" in issue
                    for issue in synchronized_state(project_root, PROCESS_ROOT, lock)
                )
            )
            self.assertEqual([], sync_skills(project_root, PROCESS_ROOT, check=False))
            repaired = target.read_text(encoding="utf-8")
            self.assertIn(
                "accepted scope is implemented without unapproved expansion",
                repaired,
            )
            self.assertIn("**Project-specific: UI evidence**", repaired)

            target.unlink()
            self.assertTrue(
                any(
                    "missing managed pull-request template" in issue
                    for issue in synchronized_state(project_root, PROCESS_ROOT, lock)
                )
            )

    def test_sync_maintains_agent_contract_and_preserves_project_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            self.prepare_project(project_root)
            (project_root / "AGENTS.md").write_text(
                "# Project rules\n\nKeep domain policy here.\n",
                encoding="utf-8",
            )
            self.assertEqual([], sync_skills(project_root, PROCESS_ROOT, check=False))
            target = project_root / "AGENTS.md"
            installed = target.read_text(encoding="utf-8")
            self.assertIn("# Project rules", installed)
            target.write_text(
                installed.replace(
                    "Independent review requires an attested read-only actor",
                    "Independent review can reuse the implementation actor",
                ),
                encoding="utf-8",
            )
            lock = validate_process_lock(
                read_json(project_root / ".process" / "process.lock")
            )

            self.assertTrue(
                any(
                    "managed agent contract differs" in issue
                    for issue in synchronized_state(project_root, PROCESS_ROOT, lock)
                )
            )
            self.assertEqual([], sync_skills(project_root, PROCESS_ROOT, check=False))
            repaired = target.read_text(encoding="utf-8")
            self.assertIn(
                "Independent review requires an attested read-only actor",
                repaired,
            )
            self.assertIn("# Project rules", repaired)

            target.write_text(
                "```markdown\n" + repaired + "```\n",
                encoding="utf-8",
            )
            self.assertTrue(
                any(
                    "raw HTML" in issue or "managed block must be visible" in issue
                    for issue in synchronized_state(project_root, PROCESS_ROOT, lock)
                )
            )

            target.write_text(
                "```markdown\n    ```\n" + repaired,
                encoding="utf-8",
            )
            self.assertTrue(
                any(
                    "raw HTML" in issue or "managed block must be visible" in issue
                    for issue in synchronized_state(project_root, PROCESS_ROOT, lock)
                )
            )

            target.write_text(
                "<pre>\n" + repaired + "</pre>\n",
                encoding="utf-8",
            )
            self.assertTrue(
                any(
                    "raw HTML" in issue or "managed block must be visible" in issue
                    for issue in synchronized_state(project_root, PROCESS_ROOT, lock)
                )
            )

            for opening_tag in ('<pre title="/>">', '<pre title="</pre>">'):
                with self.subTest(opening_tag=opening_tag):
                    target.write_text(
                        opening_tag + "\n" + repaired + "</pre>\n",
                        encoding="utf-8",
                    )
                    self.assertTrue(
                        any(
                            "raw HTML" in issue
                            or "managed block must be visible" in issue
                            for issue in synchronized_state(
                                project_root, PROCESS_ROOT, lock
                            )
                        )
                    )

            managed_only = repaired[
                repaired.index("<!-- engineering-process:start -->") :
            ]
            raw_block_wrappers = (
                ("<center>", "</center>"),
                ("<h1>", "</h1>"),
                ("<li>", "</li>"),
                ("<summary>", "</summary>"),
                ("<div/>", ""),
                ("<pre", "</pre>"),
                ("<script", "</script>"),
                ("<center", "</center>"),
                ("<source", "</source>"),
            )
            for opening_tag, closing_tag in raw_block_wrappers:
                with self.subTest(opening_tag=opening_tag):
                    target.write_text(
                        opening_tag
                        + "\n"
                        + managed_only
                        + (closing_tag + "\n" if closing_tag else ""),
                        encoding="utf-8",
                    )
                    self.assertTrue(
                        any(
                            "raw HTML" in issue
                            or "managed block must be visible" in issue
                            for issue in synchronized_state(
                                project_root, PROCESS_ROOT, lock
                            )
                        )
                    )

            raw_boundary_payloads = (
                ("<pre>", "</pre >"),
                ("<pre>", "</pre\t>"),
                ("<center>", "\u00a0"),
                ("<center>", "\u2003"),
                ("<center>", "\x0b"),
                ("<center>", "\x0c"),
            )
            for opening_tag, fake_separator in raw_boundary_payloads:
                with self.subTest(
                    opening_tag=opening_tag,
                    fake_separator=repr(fake_separator),
                ):
                    target.write_text(
                        opening_tag
                        + "\nproject policy\n"
                        + fake_separator
                        + "\n"
                        + managed_only,
                        encoding="utf-8",
                    )
                    self.assertTrue(
                        any(
                            "raw HTML" in issue
                            or "managed block must be visible" in issue
                            for issue in synchronized_state(
                                project_root, PROCESS_ROOT, lock
                            )
                        )
                    )

    def test_refuses_to_overwrite_unmanaged_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            self.prepare_project(project_root)
            target = project_root / ".agents" / "skills" / "verify-change"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("local\n", encoding="utf-8")

            with self.assertRaisesRegex(ContractError, "unmanaged"):
                sync_skills(project_root, PROCESS_ROOT, check=False)

    def test_detects_unmanaged_project_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            self.prepare_project(project_root)
            self.assertEqual([], sync_skills(project_root, PROCESS_ROOT, check=False))
            target = project_root / ".agents" / "skills" / "local-process"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("local\n", encoding="utf-8")
            lock = validate_process_lock(
                read_json(project_root / ".process" / "process.lock")
            )

            self.assertTrue(
                any(
                    "unmanaged project skill" in issue
                    for issue in synchronized_state(project_root, PROCESS_ROOT, lock)
                )
            )

            with self.assertRaisesRegex(ContractError, "unmanaged project skill"):
                sync_skills(project_root, PROCESS_ROOT, check=False)

    def test_detects_unmanaged_asset_at_skills_root(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            self.prepare_project(project_root)
            skills = project_root / ".agents" / "skills"
            skills.mkdir(parents=True)
            (skills / "README.md").write_text("local catalog\n", encoding="utf-8")
            lock = validate_process_lock(
                read_json(project_root / ".process" / "process.lock")
            )

            self.assertTrue(
                any(
                    "unmanaged project skill asset" in issue
                    for issue in synchronized_state(project_root, PROCESS_ROOT, lock)
                )
            )

    def test_relocated_install_discovers_assets_beside_the_package(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            package = target / "engineering_process"
            assets = target / "share" / "engineering-process"
            package.mkdir()
            (assets / "skills").mkdir(parents=True)
            (assets / "bundles.json").write_text("{}\n", encoding="utf-8")

            with (
                mock.patch.object(syncing, "__file__", str(package / "syncing.py")),
                mock.patch.object(syncing.sysconfig, "get_path", return_value="/missing"),
            ):
                self.assertEqual(target.resolve(), syncing.default_process_root())

    def test_synchronized_state_rejects_a_lock_without_core(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            skills = ("assess-design", "run-project-command")
            lock = ProcessLock(
                version=VERSION,
                digest=distribution_digest(PROCESS_ROOT, skills),
                skills=skills,
            )

            issues = synchronized_state(project_root, PROCESS_ROOT, lock)

            self.assertTrue(
                any("omits mandatory core skills" in issue for issue in issues)
            )
