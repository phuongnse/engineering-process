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
            "requirements-dev.txt": ["build==1.5.0", "jsonschema==4.26.0"],
            "requirements-runtime.txt": [
                "markdown-it-py==4.2.0",
                "mdurl==0.1.2",
            ],
        }
        package = resources.files("engineering_process")
        self.assertEqual(
            expected,
            {
                name: package.joinpath(name).read_text(encoding="utf-8").splitlines()
                for name in expected
            },
        )

    def test_runtime_dependency_lock_contains_exact_parser_graph(self):
        self.assertEqual(
            {
                "markdown-it-py": "4.2.0",
                "mdurl": "0.1.2",
            },
            runtime_dependency_pins(),
        )

    def test_runtime_dependency_mismatch_fails_closed(self):
        actual = {"markdown-it-py": "3.0.0", "mdurl": "0.1.2"}
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
