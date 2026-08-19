import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engineering_process import syncing
from engineering_process import VERSION
from engineering_process.contracts import ContractError, ProcessLock
from engineering_process.distribution import distribution_digest
from engineering_process.syncing import sync_skills, synchronized_state
from engineering_process.contracts import read_json, validate_process_lock


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
