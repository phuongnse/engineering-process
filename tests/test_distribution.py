import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engineering_process import distribution
from engineering_process.contracts import ContractError
from engineering_process.distribution import distribution_digest


class DistributionDigestTests(unittest.TestCase):
    def prepare_distribution(self, root: Path) -> tuple[str, ...]:
        skill = root / "process_assets" / "skills" / "sample-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\n"
            "name: sample-skill\n"
            "description: Perform a sample task when requested.\n"
            "---\n\n"
            "# Sample\n\n"
            "Perform the task.\n",
            encoding="utf-8",
        )
        (root / "schemas").mkdir()
        (root / "schemas" / "sample.schema.json").write_text(
            "{}\n", encoding="utf-8"
        )
        (root / "bundles.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "bundles": {"core": ["sample-skill"]},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "release.json").write_text(
            '{"schemaVersion":1,"version":"0.1.0"}\n', encoding="utf-8"
        )
        (root / "PRODUCTION_STANDARD.md").write_text(
            "# Production standard\n", encoding="utf-8"
        )
        (root / "VERSIONING.md").write_text(
            "# Version governance\n", encoding="utf-8"
        )
        return ("sample-skill",)

    def test_digest_covers_runtime_schema_and_selected_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = self.prepare_distribution(root)
            package = root / "runtime"
            package.mkdir()
            runtime = package / "module.py"
            runtime.write_text("VALUE = 1\n", encoding="utf-8")
            dependency_lock = package / "requirements-runtime.txt"
            dependency_lock.write_text("parser==1.0\n", encoding="utf-8")

            baseline = distribution_digest(root, selected, package_root=package)

            runtime.write_text("VALUE = 2\n", encoding="utf-8")
            runtime_changed = distribution_digest(root, selected, package_root=package)
            self.assertNotEqual(baseline, runtime_changed)

            runtime.write_text("VALUE = 1\n", encoding="utf-8")
            dependency_lock.write_text("parser==2.0\n", encoding="utf-8")
            dependency_changed = distribution_digest(
                root, selected, package_root=package
            )
            self.assertNotEqual(baseline, dependency_changed)

            dependency_lock.write_text("parser==1.0\n", encoding="utf-8")
            schema = root / "schemas" / "sample.schema.json"
            schema.write_text('{"type":"object"}\n', encoding="utf-8")
            schema_changed = distribution_digest(root, selected, package_root=package)
            self.assertNotEqual(baseline, schema_changed)

            schema.write_text("{}\n", encoding="utf-8")
            release = root / "release.json"
            release.write_text(
                '{"schemaVersion":1,"version":"0.1.1"}\n', encoding="utf-8"
            )
            release_changed = distribution_digest(
                root, selected, package_root=package
            )
            self.assertNotEqual(baseline, release_changed)

            release.write_text(
                '{"schemaVersion":1,"version":"0.1.0"}\n', encoding="utf-8"
            )
            skill = root / "process_assets" / "skills" / "sample-skill" / "SKILL.md"
            skill.write_text(skill.read_text(encoding="utf-8") + "Verify it.\n")
            skill_changed = distribution_digest(root, selected, package_root=package)
            self.assertNotEqual(baseline, skill_changed)

    def test_digest_covers_distributed_version_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = self.prepare_distribution(root)
            package = root / "runtime"
            package.mkdir()
            (package / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
            (package / "requirements-runtime.txt").write_text(
                "parser==1.0\n", encoding="utf-8"
            )
            baseline = distribution_digest(root, selected, package_root=package)

            versioning = root / "VERSIONING.md"
            versioning.write_text(
                "# Version governance\n\nDerived, never guessed.\n",
                encoding="utf-8",
            )

            self.assertNotEqual(
                baseline,
                distribution_digest(root, selected, package_root=package),
            )

    def test_digest_bounds_producer_files_entries_and_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = self.prepare_distribution(root)
            package = root / "runtime"
            package.mkdir()
            oversized = package / "module.py"
            oversized.write_bytes(
                b"x" * (distribution.MAX_DISTRIBUTION_FILE_BYTES + 1)
            )
            with self.assertRaisesRegex(ContractError, "file exceeds"):
                distribution_digest(root, selected, package_root=package)

            oversized.write_bytes(b"VALUE = 1\n")
            (package / "second.py").write_bytes(b"VALUE = 2\n")
            with (
                mock.patch.object(distribution, "MAX_DISTRIBUTION_FILES", 1),
                self.assertRaisesRegex(ContractError, "files"),
            ):
                distribution_digest(root, selected, package_root=package)

            with (
                mock.patch.object(distribution, "MAX_DISTRIBUTION_ENTRIES", 1),
                self.assertRaisesRegex(ContractError, "entries"),
            ):
                distribution_digest(root, selected, package_root=package)

            with (
                mock.patch.object(
                    distribution,
                    "DISTRIBUTION_TRAVERSAL_TIMEOUT_SECONDS",
                    -1.0,
                ),
                self.assertRaisesRegex(ContractError, "exceeded 10 seconds"),
            ):
                distribution_digest(root, selected, package_root=package)


if __name__ == "__main__":
    unittest.main()
