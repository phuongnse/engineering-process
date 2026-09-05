from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import tomllib
import unittest

from engineering_process.distribution import distribution_digest, skill_digest


def framed_digest(entries: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for relative, data in entries:
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return "sha256:" + digest.hexdigest()


class DistributionTests(unittest.TestCase):
    def test_packaged_skill_assets_match_the_source_catalog(self) -> None:
        root = Path(__file__).resolve().parent.parent
        metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        declared = {
            target: set(paths)
            for target, paths in metadata["tool"]["setuptools"]["data-files"].items()
            if target.startswith("share/engineering-process/skills/")
        }
        expected = {
            f"share/engineering-process/skills/{directory.name}": {
                path.relative_to(root).as_posix()
                for path in directory.rglob("*") if path.is_file()
            }
            for directory in (root / "process_assets" / "skills").iterdir()
            if directory.is_dir()
        }
        self.assertEqual(expected, declared)

    def test_digests_follow_canonical_relative_posix_order(self) -> None:
        entries = [
            ("skills/sample/SKILL.md", b"skill\n"),
            ("skills/sample/invariants.json", b"{}\n"),
            ("schemas/sample.json", b"{}\n"),
            ("templates/AGENTS.process.md", b"agents\n"),
            ("templates/PULL_REQUEST_TEMPLATE.md", b"pull request\n"),
            ("templates/adopt-process.py", b"adopt\n"),
            ("templates/adopt-process-windows-job.py", b"windows\n"),
            ("process-graph.json", b"{}\n"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative, data in entries:
                path = root / relative.replace("skills/", "process_assets/skills/", 1)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)

            self.assertEqual(framed_digest(entries), distribution_digest(root))
            self.assertEqual(
                framed_digest(
                    [
                        ("SKILL.md", b"skill\n"),
                        ("invariants.json", b"{}\n"),
                    ]
                ),
                skill_digest(root / "process_assets" / "skills" / "sample"),
            )


if __name__ == "__main__":
    unittest.main()
