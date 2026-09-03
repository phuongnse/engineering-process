from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "engineering_process"
LAYERS = {
    "__init__": 0,
    "_supervisor_contract": 0,
    "_windows_job": 0,
    "contracts": 0,
    "helper_launch": 0,
    "_supervisor_posix": 1,
    "_supervisor_windows": 1,
    "distribution": 1,
    "repository": 1,
    "project": 2,
    "production_engineering": 2,
    "publication_compat": 2,
    "release": 2,
    "skills": 2,
    "supervision": 2,
    "commands": 3,
    "adoption": 4,
    "lifecycle": 4,
    "cli": 5,
    "__main__": 6,
}
TRANSITIONS = {
    "start_change",
    "register_plan",
    "begin_implementation",
    "verify_change",
    "start_review",
    "submit_review",
    "finish_change",
}


def _modules() -> dict[str, Path]:
    return {path.stem: path for path in RUNTIME.glob("*.py")}


def _dependencies_from_source(source: str, modules: set[str]) -> set[str]:
    dependencies: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.module:
                targets = {node.module.split(".")[0]}
            elif node.level or node.module == "engineering_process":
                targets = {
                    alias.name.split(".")[0]
                    if alias.name.split(".")[0] in modules
                    else "__init__"
                    for alias in node.names
                }
            elif node.module and node.module.startswith("engineering_process."):
                parts = node.module.split(".")
                targets = {parts[1]}
            else:
                continue
            dependencies.update(target for target in targets if target in modules)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "engineering_process":
                    target = parts[1] if len(parts) > 1 else "__init__"
                    if target in modules:
                        dependencies.add(target)
    return dependencies


def _dependencies(path: Path, modules: set[str]) -> set[str]:
    return _dependencies_from_source(path.read_text(encoding="utf-8"), modules)


class ArchitectureTests(unittest.TestCase):
    def test_package_root_imports_cannot_hide_dependencies(self) -> None:
        modules = set(LAYERS)
        fixtures = {
            "from . import lifecycle": {"lifecycle"},
            "from engineering_process import commands": {"commands"},
            "from . import VERSION": {"__init__"},
            "from engineering_process import VERSION": {"__init__"},
        }
        for source, expected in fixtures.items():
            with self.subTest(source=source):
                self.assertEqual(expected, _dependencies_from_source(source, modules))

    def test_runtime_dependencies_are_layered_and_acyclic(self) -> None:
        modules = _modules()
        self.assertEqual(set(LAYERS), set(modules), "classify every runtime module")
        graph = {
            name: _dependencies(path, set(modules))
            for name, path in modules.items()
        }
        for source, dependencies in graph.items():
            for target in dependencies:
                self.assertLess(
                    LAYERS[target],
                    LAYERS[source],
                    f"{source} must not depend on same or higher layer {target}",
                )
            reachable: set[str] = set()
            pending = list(dependencies)
            while pending:
                target = pending.pop()
                if target not in reachable:
                    reachable.add(target)
                    pending.extend(graph[target])
            self.assertNotIn(source, reachable, f"dependency cycle reaches {source}")

    def test_lifecycle_owns_every_state_transition(self) -> None:
        modules = _modules()
        definitions = {
            node.name: module
            for module, path in modules.items()
            for node in ast.parse(path.read_text(encoding="utf-8")).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in TRANSITIONS
        }
        self.assertEqual({name: "lifecycle" for name in TRANSITIONS}, definitions)
        importers = {
            module
            for module, path in modules.items()
            if "lifecycle" in _dependencies(path, set(modules))
        }
        self.assertEqual({"cli"}, importers)

    def test_removed_governance_subsystems_do_not_return(self) -> None:
        removed_modules = {
            "artifact_attestation.py",
            "evidence_transport.py",
            "improvement.py",
            "publication.py",
            "recommendation.py",
            "remote_verification.py",
            "supplemental.py",
            "transition.py",
        }
        actual = {path.name for path in RUNTIME.glob("*.py")}
        self.assertTrue(removed_modules.isdisjoint(actual))
        workflows = {path.name for path in (ROOT / ".github" / "workflows").glob("*.yml")}
        self.assertEqual({"ci.yml", "publish.yml", "release-pr.yml"}, workflows)

    def test_source_skills_have_no_orphaned_legacy_directories(self) -> None:
        skills = {
            path.parent.name
            for path in (ROOT / "process_assets" / "skills").glob("*/SKILL.md")
        }
        self.assertNotIn("publish-change", skills)
        self.assertNotIn("cross-repo-change", skills)


if __name__ == "__main__":
    unittest.main()
