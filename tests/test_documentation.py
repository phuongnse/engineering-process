from __future__ import annotations

from pathlib import Path
import re
import unittest

from engineering_process.artifact_standards import resolve_standard
from engineering_process.contracts import formatted_json_bytes
from engineering_process.distribution import distribution_root
from engineering_process.pr_description import render_renovate_preset, render_template


ROOT = Path(__file__).resolve().parent.parent
LOCAL_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def markdown_files() -> list[Path]:
    paths = [
        ROOT / "README.md",
        ROOT / "AGENTS.md",
        ROOT / "ARTIFACT_STANDARDS.md",
        ROOT / "PROCESS_IMPROVEMENT.md",
        ROOT / "SELF_HOSTING.md",
        ROOT / "VERSIONING.md",
        ROOT / "RELEASING.md",
        ROOT / "RELEASE_NOTES.md",
        ROOT / "release-changes" / "README.md",
    ]
    paths.extend(sorted((ROOT / "docs").glob("*.md")))
    paths.extend(sorted((ROOT / "process_assets" / "skills").glob("*/SKILL.md")))
    return paths


class DocumentationTests(unittest.TestCase):
    def test_reader_facing_local_links_resolve(self) -> None:
        for source in markdown_files():
            text = source.read_text(encoding="utf-8")
            for target in LOCAL_LINK.findall(text):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                relative = target.split("#", 1)[0]
                if not relative:
                    continue
                destination = (source.parent / relative).resolve()
                with self.subTest(source=source.relative_to(ROOT), target=target):
                    self.assertTrue(destination.is_file())

    def test_readme_exposes_each_reader_route(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for route in (
            "docs/consumer-setup.md",
            "docs/delivery.md",
            "docs/documentation.md",
            "PROCESS_IMPROVEMENT.md",
            "SELF_HOSTING.md",
            "ARTIFACT_STANDARDS.md",
            "VERSIONING.md",
            "RELEASING.md",
        ):
            with self.subTest(route=route):
                self.assertIn(route, readme)
        self.assertLess(len(readme.splitlines()), 240)

    def test_documentation_guidance_is_at_the_lifecycle_boundaries(self) -> None:
        roots = ROOT / "process_assets" / "skills"
        expected = {
            "deliver-change": ("docs/documentation.md", "authoritative source"),
            "change-start": ("reader groups could be affected", "no-impact decision"),
            "change-plan": ("generated or published output", "reader consequence"),
            "change-implement": ("regenerate the output", "no reader impact"),
            "change-verify": ("reader-facing claim", "source-to-output"),
            "change-review": ("representative reader", "hard-to-use information"),
        }
        for skill, fragments in expected.items():
            text = (roots / skill / "SKILL.md").read_text(encoding="utf-8")
            for fragment in fragments:
                with self.subTest(skill=skill, fragment=fragment):
                    self.assertIn(fragment, text)

    def test_packaged_standard_and_derived_template_outputs_match(self) -> None:
        standard = resolve_standard(None, distribution_root(), "pull-request")
        self.assertEqual(
            render_template(standard).encode("utf-8"),
            (ROOT / "templates" / "PULL_REQUEST_TEMPLATE.md").read_bytes(),
        )
        self.assertEqual(
            formatted_json_bytes(render_renovate_preset(standard)),
            (ROOT / "templates" / "renovate.json").read_bytes(),
        )

    def test_documentation_source_map_names_adoption_boundary(self) -> None:
        text = (ROOT / "docs" / "documentation.md").read_text(encoding="utf-8")
        for fragment in (
            "process_assets/skills",
            ".agents/skills after consumer adoption",
            "source definition and generator are authoritative",
            "currently adopted managed",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)


if __name__ == "__main__":
    unittest.main()
