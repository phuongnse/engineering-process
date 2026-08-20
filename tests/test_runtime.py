import unittest
from unittest import mock
from importlib import resources

from engineering_process.contracts import ContractError
from engineering_process.runtime import (
    assert_runtime_dependencies,
    runtime_dependency_pins,
)


class RuntimeDependencyTests(unittest.TestCase):
    def test_all_distribution_dependency_locks_are_exact(self):
        expected = {
            "requirements-build.txt": ["setuptools==84.0.0"],
            "requirements-dev.txt": [
                "attrs==26.1.0",
                "build==1.5.0",
                'colorama==0.4.6; os_name == "nt"',
                "jsonschema==4.26.0",
                "jsonschema-specifications==2025.9.1",
                "packaging==26.3",
                "pyproject_hooks==1.2.0",
                "referencing==0.37.0",
                "rpds-py==2026.6.3",
                'typing-extensions==4.15.0; python_version < "3.13"',
            ],
            "requirements-runtime.txt": [
                "markdown-it-py==4.2.0",
                "mdurl==0.1.2",
                "regex==2026.7.19",
            ],
        }
        package = resources.files("engineering_process")
        self.assertEqual(
            set(expected) | {"requirements-release.txt"},
            {
                item.name
                for item in package.iterdir()
                if item.name.startswith("requirements-") and item.name.endswith(".txt")
            },
        )
        self.assertEqual(
            expected,
            {
                name: package.joinpath(name).read_text(encoding="utf-8").splitlines()
                for name in expected
            },
        )

    def test_release_dependency_graph_is_hash_locked(self):
        package = resources.files("engineering_process")
        lines = package.joinpath("requirements-release.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual("--only-binary :all:", lines[0])
        requirements = [
            line for line in lines if line and not line.startswith(("#", " "))
        ]
        self.assertGreater(len(requirements), 1)
        for index, requirement in enumerate(requirements[1:], start=1):
            with self.subTest(requirement=requirement):
                self.assertRegex(requirement, r"^[a-z0-9-]+==[^ ]+ \\$" )
                line_index = lines.index(requirement)
                self.assertRegex(
                    lines[line_index + 1], r"^    --hash=sha256:[0-9a-f]{64}$"
                )

    def test_runtime_dependency_lock_contains_exact_parser_graph(self):
        self.assertEqual(
            {
                "markdown-it-py": "4.2.0",
                "mdurl": "0.1.2",
                "regex": "2026.7.19",
            },
            runtime_dependency_pins(),
        )

    def test_runtime_dependency_mismatch_fails_closed(self):
        actual = {
            "markdown-it-py": "3.0.0",
            "mdurl": "0.1.2",
            "regex": "2026.7.19",
        }
        with mock.patch(
            "engineering_process.runtime.metadata.version",
            side_effect=lambda name: actual[name],
        ):
            with self.assertRaisesRegex(
                ContractError, "markdown-it-py is 3.0.0; expected 4.2.0"
            ):
                assert_runtime_dependencies()


if __name__ == "__main__":
    unittest.main()
