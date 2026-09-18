from __future__ import annotations

import ast
import json
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
    "evidence": 2,
    "repository": 1,
    "project": 2,
    "impact": 3,
    "artifact_standards": 2,
    "automation_name": 3,
    "issue": 3,
    "production_engineering": 2,
    "source_publication": 2,
    "review_contexts": 2,
    "incidents": 3,
    "pr_description": 3,
    "release_notes": 3,
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
            "publication_compat.py",
        }
        actual = {path.name for path in RUNTIME.glob("*.py")}
        self.assertTrue(removed_modules.isdisjoint(actual))
        self.assertFalse((ROOT / "schemas" / "project-legacy.schema.json").exists())
        workflows = {path.name for path in (ROOT / ".github" / "workflows").glob("*.yml")}
        self.assertEqual({"ci.yml", "publish.yml", "release-pr.yml"}, workflows)

    def test_source_skills_have_no_orphaned_directories(self) -> None:
        skills = {
            path.parent.name
            for path in (ROOT / "process_assets" / "skills").glob("*/SKILL.md")
        }
        self.assertNotIn("publish-change", skills)
        self.assertNotIn("cross-repo-change", skills)

    def test_process_owned_contract_inventory_has_one_current_version(self) -> None:
        schemas = ROOT / "schemas"
        for path in sorted(schemas.glob("*.schema.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(path=path.name):
                schema_version = document.get("properties", {}).get("schemaVersion")
                if schema_version is not None:
                    self.assertEqual({"const": 1}, schema_version)
        for path in sorted((ROOT / "process_assets" / "standards").glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(path=path.name):
                self.assertEqual(1, document["schemaVersion"])
                self.assertEqual(1, document["version"])
        for path in (
            ROOT / "process-graph.json",
            ROOT / "release.json",
        ):
            document = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self.assertEqual(1, document["schemaVersion"])

    def test_runtime_is_agent_neutral(self) -> None:
        forbidden_brand_substrings = (
            "antigravity",
            "copilot",
            "windsurf",
            "claude",
            "chatgpt",
            "gemini",
        )
        for path in RUNTIME.glob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            for brand in forbidden_brand_substrings:
                self.assertNotIn(
                    brand,
                    text,
                    f"{path.name} must remain agent-neutral and not reference vendor brand {brand!r}",
                )

    def test_skills_are_agent_neutral(self) -> None:
        forbidden_brand_substrings = (
            "antigravity",
            "copilot",
            "windsurf",
            "claude",
            "chatgpt",
            "gemini",
        )
        for path in (ROOT / "process_assets" / "skills").glob("**/*.md"):
            text = path.read_text(encoding="utf-8").lower()
            for brand in forbidden_brand_substrings:
                self.assertNotIn(
                    brand,
                    text,
                    f"{path.name} must remain agent-neutral and not reference vendor brand {brand!r}",
                )

    def test_execution_identity_never_reads_ambient_environment(self) -> None:
        source = (RUNTIME / "evidence.py").read_text(encoding="utf-8")
        module_ast = ast.parse(source)
        fn_node = next(
            (
                node
                for node in ast.walk(module_ast)
                if isinstance(node, ast.FunctionDef) and node.name == "execution_identity"
            ),
            None,
        )
        self.assertIsNotNone(fn_node, "execution_identity function must exist in evidence.py")
        assert fn_node is not None
        for child in ast.walk(fn_node):
            if isinstance(child, ast.Attribute) and child.attr == "environ":
                self.fail(
                    "execution_identity must not access os.environ; execution identity inputs must be explicit, not ambient"
                )
            if isinstance(child, ast.Name) and child.id == "environ":
                self.fail(
                    "execution_identity must not access environ; execution identity inputs must be explicit, not ambient"
                )


if __name__ == "__main__":
    unittest.main()
