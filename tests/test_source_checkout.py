import subprocess
import unittest
from pathlib import Path


PROCESS_ROOT = Path(__file__).resolve().parent.parent
PRODUCER_ATTRIBUTES = (
    b"* text=auto eol=lf -working-tree-encoding -filter -ident\n"
)
REPRESENTATIVE_TEXT_PATHS = (
    ".gitattributes",
    "engineering_process/git_attributes.py",
    "process_assets/skills/verify-change/SKILL.md",
    "schemas/project.schema.json",
    "templates/AGENTS.process.md",
)


class SourceCheckoutTests(unittest.TestCase):
    def test_producer_text_sources_have_byte_stable_lf_checkout(self):
        self.assertEqual(
            PRODUCER_ATTRIBUTES,
            (PROCESS_ROOT / ".gitattributes").read_bytes(),
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
                *REPRESENTATIVE_TEXT_PATHS,
            ],
            cwd=PROCESS_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        for path in REPRESENTATIVE_TEXT_PATHS:
            with self.subTest(path=path):
                self.assertIn(f"{path}: text: auto", effective)
                self.assertIn(f"{path}: eol: lf", effective)
                self.assertIn(
                    f"{path}: working-tree-encoding: unset", effective
                )
                self.assertIn(f"{path}: filter: unset", effective)
                self.assertIn(f"{path}: ident: unset", effective)
                self.assertNotIn(b"\r\n", (PROCESS_ROOT / path).read_bytes())

        for path in (PROCESS_ROOT / "process_assets" / "skills").rglob("*"):
            if path.is_file():
                with self.subTest(managed_source=path.relative_to(PROCESS_ROOT)):
                    self.assertNotIn(b"\r\n", path.read_bytes())
