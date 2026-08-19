import json
import tempfile
import unittest
from pathlib import Path

from engineering_process.distribution import distribution_digest


class DistributionDigestTests(unittest.TestCase):
    def prepare_distribution(self, root: Path) -> tuple[str, ...]:
        skill = root / ".agents" / "skills" / "sample-skill"
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
            skill = root / ".agents" / "skills" / "sample-skill" / "SKILL.md"
            skill.write_text(skill.read_text(encoding="utf-8") + "Verify it.\n")
            skill_changed = distribution_digest(root, selected, package_root=package)
            self.assertNotEqual(baseline, skill_changed)


if __name__ == "__main__":
    unittest.main()
