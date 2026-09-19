from __future__ import annotations

from pathlib import Path
from copy import deepcopy
import contextlib
import io
import json
import shutil
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from engineering_process.artifact_standards import resolve_standard
from engineering_process.contracts import ProcessError, read_json, validate_document, write_json_atomic
from engineering_process.distribution import schemas_root
from engineering_process.release import derive_next_version, validate_release
from verification.normalize_sdist import normalize
from verification.prepare_release import _replace_once
from verification import render_release_notes as notes_renderer
from verification.verify_distribution import validate_distribution_text


ROOT = Path(__file__).resolve().parent.parent


def _current_release(changes: list[dict]) -> dict:
    details = {
        "problem": "The release record needs a structured explanation.",
        "changes": "Render the record with the new readable release format.",
        "affectedPaths": ["tests/test_release.py"],
        "apply": "Use the generated release body.",
        "compatibility": "No breaking change.",
        "notes": "This is a synthetic current release record.",
    }
    return {
         "schemaVersion": 1,
        "version": "3.0.0",
        "previousVersion": "2.1.0",
        "changes": [{**change, "details": deepcopy(details)} for change in changes],
    }


class ReleaseTests(unittest.TestCase):
    def test_notes_group_every_change_and_keep_sources_and_upgrade_context(self) -> None:
        details = {
            "problem": "A current release record needs a structured explanation.",
            "changes": "Record the observable release behavior.",
            "affectedPaths": ["engineering_process/release.py"],
            "apply": "Adopt the released package.",
            "compatibility": "No breaking change.",
            "notes": "This is a synthetic current release record.",
        }
        release = {
            "schemaVersion": 1, "version": "3.0.0", "previousVersion": "2.1.0",
            "changes": [
                {"id": kind, "type": kind, "summary": f"Observable {kind} behavior", "source": f"https://github.com/phuongnse/engineering-process/issues/{number}", "details": deepcopy(details)}
                for number, kind in enumerate(("fix", "capability", "breaking"), 1)
            ],
        }
        notes = notes_renderer.render_release_notes(release)
        self.assertLess(notes.index("## Breaking changes"), notes.index("## Features"))
        self.assertLess(notes.index("## Features"), notes.index("## Fixes"))
        for number, change in enumerate(release["changes"], 1):
            self.assertEqual(1, notes.count(change["summary"]))
            self.assertIn(f"[#{number}]({change['source']})", notes)
        self.assertIn("compare/v2.1.0...v3.0.0", notes)
        self.assertIn("blob/v3.0.0/VERSIONING.md", notes)
        self.assertIn("Consumer CI", notes)
        self.assertNotIn("\r", notes)

    def test_detailed_notes_explain_each_change_and_breaking_impact(self) -> None:
        release = {
            "schemaVersion": 1, "version": "3.0.0", "previousVersion": "2.1.0",
            "changes": [{
                "id": "selection",
                "type": "capability",
                "summary": "Select necessary work",
                "source": "https://github.com/phuongnse/engineering-process/issues/205",
                "details": {
                    "problem": "The same profile was dispatched more than once.",
                    "changes": "Reuse valid whole-profile evidence and run only remaining work.",
                    "affectedPaths": ["engineering_process/lifecycle.py", "schemas/run.schema.json"],
                    "apply": "Run change explain before change verify --remaining.",
                    "compatibility": "No breaking change; explicit profile refresh remains available.",
                    "notes": "Unknown evidence still reruns conservatively."
                }
            }]
        }
        notes = notes_renderer.render_release_notes(release)
        labels = resolve_standard(ROOT, ROOT, "release-notes").rules["detailLabels"]
        self.assertIn("**Select necessary work**", notes)
        self.assertIn("([#205](https://github.com/phuongnse/engineering-process/issues/205))", notes)
        for field in ("changes", "apply", "compatibility"):
            self.assertIn(f"**{labels[field]}:", notes)
        for field in ("problem", "affectedPaths", "notes"):
            self.assertNotIn(f"**{labels[field]}:", notes)
        self.assertNotIn("`engineering_process/lifecycle.py`", notes)
        release["changes"][0]["details"]["apply"] = "pending"
        with self.assertRaisesRegex(ProcessError, "unresolved value"):
            notes_renderer.render_release_notes(release)

    def test_detail_projection_matches_change_reader_need(self) -> None:
        labels = resolve_standard(ROOT, ROOT, "release-notes").rules["detailLabels"]
        notes = notes_renderer.render_release_notes(
            _current_release([
                {"id": "ordinary", "type": "fix", "summary": "Ordinary fix", "source": "https://example.invalid/changes/1"},
                {"id": "feature", "type": "capability", "summary": "New capability", "source": "https://example.invalid/changes/2"},
                {"id": "break", "type": "breaking", "summary": "Breaking boundary", "source": "https://example.invalid/changes/3"},
            ])
        )
        breaking = notes[notes.index("**Breaking boundary**"):notes.index("## Features")]
        capability = notes[notes.index("**New capability**"):notes.index("## Fixes")]
        ordinary = notes[notes.index("**Ordinary fix**"):notes.index("## Upgrade")]
        self.assertIn(f"**{labels['changes']}:", ordinary)
        self.assertIn(f"**{labels['compatibility']}:", ordinary)
        self.assertNotIn(f"**{labels['affectedPaths']}:", ordinary)
        self.assertNotIn(f"**{labels['notes']}:", ordinary)
        self.assertIn(f"**{labels['apply']}:", capability)
        self.assertIn(f"**{labels['problem']}:", breaking)
        self.assertIn(f"**{labels['apply']}:", breaking)
        self.assertIn(f"**{labels['compatibility']}:", breaking)

    def test_notes_treat_metadata_as_text_and_do_not_invent_source_links(self) -> None:
        release = _current_release([
            {"id": "safe-text", "type": "fix", "summary": "Cải thiện `tool`\n# heading [link]", "source": "https://example.invalid/changes/42"},
            {"id": "safe-url", "type": "fix", "summary": "Safe source link.", "source": "https://example.invalid/a%29%20bad"},
        ])
        notes = notes_renderer.render_release_notes(release)
        self.assertIn("Cải thiện \\`tool\\` # heading \\[link\\]", notes)
        self.assertNotIn("\n# heading", notes)
        self.assertIn("[Source](https://example.invalid/changes/42)", notes)
        self.assertIn("https://example.invalid/a%29%20bad", notes)
        self.assertNotIn("## Features", notes)
        release["changes"][0]["summary"] = "### heading"
        notes = notes_renderer.render_release_notes(release)
        self.assertIn(r"- **\### heading**", notes)
        self.assertNotIn("\n### heading", notes)

    def test_notes_preserve_list_markers_entities_and_strikethrough_as_text(self) -> None:
        release = _current_release([{
            "id": "literal-metadata", "type": "fix",
            "summary": "1. Preserve literal &copy; <tag> and ~~removed~~ labels.",
            "source": "https://example.invalid/changes/42",
        }])
        notes = notes_renderer.render_release_notes(release)
        self.assertIn(r"- **1. Preserve literal &amp;copy; &lt;tag&gt; and \~\~removed\~\~ labels.**", notes)
        self.assertIn("[Source](https://example.invalid/changes/42)", notes)
        self.assertNotIn(r"\.", notes)
        self.assertNotIn(r"\-", notes)
        for marker in ("-", "+", "*"):
            release["changes"][0]["summary"] = marker + " Preserve the literal bullet marker"
            with self.subTest(marker=marker):
                self.assertIn("- **\\" + marker + " Preserve", notes_renderer.render_release_notes(release))

    def test_source_urls_are_rendered_as_links(self) -> None:
        release = _current_release([{
            "id": "literal-reference",
            "type": "fix",
            "summary": "Keep the source literal",
            "source": "https://example.invalid/changes/%60owned%60",
        }])
        self.assertIn(
            "[Source](https://example.invalid/changes/%60owned%60)",
            notes_renderer.render_release_notes(release),
        )

    def test_plain_text_issue_sources_are_rejected(self) -> None:
        release = _current_release([{
            "id": "plain-reference",
            "type": "fix",
            "summary": "Reject an unlinked issue reference",
            "source": "component (issue #42)",
        }])
        with self.assertRaisesRegex(ProcessError, "does not match"):
            notes_renderer.render_release_notes(release)

    def test_canonical_release_contracts_reject_plain_text_sources(self) -> None:
        release = _current_release([{
            "id": "plain-reference",
            "type": "fix",
            "summary": "Reject an unlinked issue reference",
            "source": "component (issue #42)",
        }])
        fragment = deepcopy(release["changes"][0])
        for kind, document in (("release", release), ("release-change", fragment)):
            with self.subTest(kind=kind), self.assertRaisesRegex(ProcessError, "does not match"):
                validate_document(document, kind, schema_root=schemas_root(ROOT))

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            release_copy = deepcopy(read_json(ROOT / "release.json"))
            release_copy["changes"][0]["source"] = "issue #1"
            (project / "release.json").write_text(json.dumps(release_copy), encoding="utf-8")
            shutil.copyfile(ROOT / "pyproject.toml", project / "pyproject.toml")
            with self.assertRaisesRegex(ProcessError, "does not match"):
                validate_release(project, ROOT)

    def test_notes_check_rejects_stale_missing_and_noncanonical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            target = Path(directory) / "notes.md"
            self.assertEqual(0, notes_renderer.main(["--output", str(target)]))
            expected = target.read_bytes()
            self.assertEqual(0, notes_renderer.main(["--check", str(target)]))
            for invalid in (b"stale\n", expected.replace(b"\n", b"\r\n"), b"\xef\xbb\xbf" + expected):
                target.write_bytes(invalid)
                with self.subTest(invalid=invalid[:16]), self.assertRaisesRegex(ProcessError, "stale"):
                    notes_renderer.main(["--check", str(target)])
            target.unlink()
            with self.assertRaises(OSError):
                notes_renderer.main(["--check", str(target)])

    def test_notes_validate_the_manifest_before_overwriting_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "schemas").mkdir()
            shutil.copyfile(ROOT / "schemas/release.schema.json", root / "schemas/release.schema.json")
            release = deepcopy(read_json(ROOT / "release.json"))
            release["changes"][0]["type"] = "unknown"
            (root / "release.json").write_text(json.dumps(release), encoding="utf-8")
            target = root / "notes.md"
            target.write_bytes(b"preserved\n")
            with patch.object(notes_renderer, "PROJECT_ROOT", root), self.assertRaises(ProcessError):
                notes_renderer.main(["--output", str(target)])
            self.assertEqual(b"preserved\n", target.read_bytes())

    def test_release_replacement_writes_explicit_lf_on_every_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "version.txt"
            for source in (b"version=old\nnext\n", b"version=old\r\nnext\r\n", b"version=old\r\nnext\n"):
                with self.subTest(source=source):
                    target.write_bytes(source)
                    _replace_once(target, "old", "new")
                    self.assertEqual(b"version=new\nnext\n", target.read_bytes())

    def test_release_replacement_rejects_bom_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "version.txt"
            original = b'\xef\xbb\xbfVERSION = "old"\r\n'
            target.write_bytes(original)
            with self.assertRaisesRegex(ProcessError, "UTF-8 without BOM"):
                _replace_once(target, "old", "new")
            self.assertEqual(original, target.read_bytes())
            self.assertFalse(target.with_name(f".{target.name}.release.tmp").exists())

    def test_distribution_text_uses_the_declared_inventory_and_rejects_byte_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "engineering_process").mkdir()
            (root / "assets").mkdir()
            (root / "pyproject.toml").write_bytes(b'[tool.setuptools.data-files]\n"share/process" = ["assets/newly-declared.fragment"]\n')
            (root / "release.json").write_bytes(b"{}\n")
            (root / "engineering_process" / "__init__.py").write_bytes(b'VERSION = "1.0.0"\n')
            (root / "opaque.bin").write_bytes(b"\x00\xff\r\n")
            target = root / "assets" / "newly-declared.fragment"
            valid = b"first\nsecond\n"
            target.write_bytes(valid)
            validate_distribution_text(root)
            for invalid in (b"first\r\nsecond\r\n", b"first\r\nsecond\n", b"\xef\xbb\xbf" + valid, b"\xff\n"):
                with self.subTest(invalid=invalid):
                    target.write_bytes(invalid)
                    with self.assertRaisesRegex(RuntimeError, "UTF-8 without BOM and use LF"):
                        validate_distribution_text(root)
                    target.write_bytes(valid)
                    validate_distribution_text(root)

    def test_current_release_identity_is_consistent(self) -> None:
        release = read_json(ROOT / "release.json")
        result = validate_release(ROOT, ROOT, tag=f"v{release['version']}")
        self.assertEqual(release["version"], result["version"])
        self.assertEqual(len(release["changes"]), result["changeCount"])
        self.assertEqual(notes_renderer.render_release_notes(release).encode("utf-8"), (ROOT / "RELEASE_NOTES.md").read_bytes())

    def test_current_release_records_are_issue_level_and_complete(self) -> None:
        release = read_json(ROOT / "release.json")
        self.assertEqual(1, release["schemaVersion"])
        self.assertTrue(all("details" in change for change in release["changes"]))
        required_details = {
            "problem", "changes", "affectedPaths", "apply", "compatibility", "notes"
        }
        self.assertTrue(
            all(required_details == set(change["details"]) for change in release["changes"])
        )
        notes = notes_renderer.render_release_notes(release)
        for change in release["changes"]:
            self.assertIn(f"]({change['source']})", notes)

    def test_pending_release_records_are_issue_level_and_complete(self) -> None:
        fragments = [
            read_json(path)
            for path in sorted((ROOT / "release-changes").glob("*.json"))
        ]
        if not fragments:
            self.skipTest("pending release records have been consumed")
        self.assertTrue(all(fragment["schemaVersion"] == 1 for fragment in fragments))
        self.assertTrue(all("details" in fragment for fragment in fragments))
        sources = {fragment["source"] for fragment in fragments}
        self.assertEqual(len(sources), len(fragments))
        release = {
            "schemaVersion": 1,
            "version": "2.5.0",
            "previousVersion": read_json(ROOT / "release.json")["version"],
            "changes": [
                {
                    key: fragment[key]
                    for key in ("id", "type", "summary", "source", "details")
                }
                for fragment in fragments
            ],
        }
        notes = notes_renderer.render_release_notes(release)
        for source in sources:
            self.assertIn(f"]({source})", notes)
        if any(fragment["type"] == "breaking" for fragment in fragments):
            self.assertIn("Breaking changes are listed above", notes)
        else:
            self.assertIn("No breaking changes are included", notes)

    def test_semver_is_derived_from_change_classification(self) -> None:
        self.assertEqual("0.9.1", derive_next_version("0.9.0", ["fix"]))
        self.assertEqual("0.10.0", derive_next_version("0.9.0", ["capability"]))
        self.assertEqual("1.0.0", derive_next_version("0.9.0", ["breaking"]))
        self.assertEqual("3.0.0", derive_next_version("2.4.1", ["fix", "breaking"]))

    def test_release_change_classification_matches_live_state(self) -> None:
        release = read_json(ROOT / "release.json")
        pending = [
            read_json(path)
            for path in sorted((ROOT / "release-changes").glob("*.json"))
        ]
        if pending:
            derived = derive_next_version(
                release["version"], (fragment["type"] for fragment in pending)
            )
            self.assertNotEqual(release["version"], derived)
        else:
            derived = derive_next_version(
                release["previousVersion"],
                (change["type"] for change in release["changes"]),
            )
            self.assertEqual(release["version"], derived)

    def test_release_preparation_materializes_next_version_in_a_copy(self) -> None:
        current = read_json(ROOT / "release.json")
        expected = derive_next_version(current["version"], ["fix"])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source"
            shutil.copytree(
                ROOT,
                target,
                ignore=shutil.ignore_patterns(
                    ".git", ".venv", ".process", "build", "*.egg-info", "__pycache__"
                ),
            )
            for path in (target / "release-changes").glob("*.json"):
                path.unlink()
            write_json_atomic(
                target / "release-changes" / "test-fix.json",
                {
                    "schemaVersion": 1,
                    "id": "test-fix",
                    "type": "fix",
                    "summary": "Exercise release preparation against live state.",
                    "source": "https://example.invalid/fixtures/release-test",
                    "details": {
                        "problem": "The fixture needs a complete release record.",
                        "changes": "Supply the required structured release details.",
                        "affectedPaths": ["tests/test_release.py"],
                        "apply": "Run the normal release preparation command.",
                        "compatibility": "No breaking change.",
                        "notes": "This is test-only release metadata."
                    },
                },
            )
            result = subprocess.run(
                [sys.executable, "verification/prepare_release.py", expected],
                cwd=target,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            prepared = read_json(target / "release.json")
            self.assertEqual(expected, prepared["version"])
            self.assertEqual(current["version"], prepared["previousVersion"])
            self.assertEqual(1, prepared["schemaVersion"])
            self.assertEqual("The fixture needs a complete release record.", prepared["changes"][0]["details"]["problem"])
            self.assertEqual([], list((target / "release-changes").glob("*.json")))
            generated_notes = notes_renderer.render_release_notes(prepared).encode("utf-8")
            self.assertEqual(generated_notes, (target / "RELEASE_NOTES.md").read_bytes())
            labels = resolve_standard(ROOT, ROOT, "release-notes").rules["detailLabels"]
            self.assertIn(f"**{labels['changes']}:".encode("utf-8"), generated_notes)
            self.assertIn(
                f'version = "{expected}"', (target / "pyproject.toml").read_text()
            )
            validate_distribution_text(target)

    def test_release_preparation_rejects_non_current_fragments_before_writing(self) -> None:
        current = read_json(ROOT / "release.json")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source"
            shutil.copytree(
                ROOT,
                target,
                ignore=shutil.ignore_patterns(
                    ".git", ".venv", ".process", "build", "*.egg-info", "__pycache__"
                ),
            )
            for path in (target / "release-changes").glob("*.json"):
                path.unlink()
            write_json_atomic(
                target / "release-changes" / "unsupported.json",
                {
                    "schemaVersion": 2,
                    "id": "unsupported",
                    "type": "fix",
                    "summary": "Unsupported record.",
                    "source": "https://example.invalid/fixtures/unsupported",
                    "details": {
                        "problem": "The fixture uses a superseded version.",
                        "changes": "Reject it before writing release files.",
                        "affectedPaths": ["tests/test_release.py"],
                        "apply": "Use the current release contract.",
                        "compatibility": "Breaking contract boundary.",
                        "notes": "This is test-only metadata."
                    },
                },
            )
            original_notes = (target / "RELEASE_NOTES.md").read_bytes()
            result = subprocess.run(
                [sys.executable, "verification/prepare_release.py", "2.4.1"],
                cwd=target,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("1 was expected", result.stderr)
            self.assertEqual(current["version"], read_json(target / "release.json")["version"])
            self.assertEqual(original_notes, (target / "RELEASE_NOTES.md").read_bytes())

    def test_invalid_change_type_fails(self) -> None:
        with self.assertRaises(ProcessError):
            derive_next_version("0.9.0", ["governance"])

    def test_release_files_are_reproducible_across_checkout_mtimes(self) -> None:
        epoch = 1_700_000_000
        built: list[dict[str, str]] = []
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            for index in range(2):
                source = temporary / f"source-{index}"
                output = temporary / f"dist-{index}"
                shutil.copytree(
                    ROOT,
                    source,
                    ignore=shutil.ignore_patterns(
                        ".git", ".venv", ".process", "build", "dist", "*.egg-info", "__pycache__"
                    ),
                )
                for path in source.rglob("*"):
                    if path.is_file():
                        os.utime(path, (epoch + index * 10_000,) * 2)
                result = subprocess.run(
                    [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(output)],
                    cwd=source,
                    env={**os.environ, "SOURCE_DATE_EPOCH": str(epoch), "PYTHONHASHSEED": "0"},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                sdist = next(output.glob("*.tar.gz"))
                normalize(sdist, epoch)
                built.append(
                    {
                        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in output.iterdir()
                    }
                )
        self.assertEqual(built[0], built[1])


if __name__ == "__main__":
    unittest.main()
