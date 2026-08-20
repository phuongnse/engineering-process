import re
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from markdown_it import MarkdownIt

from engineering_process.markdown import visible_markdown_links


PROCESS_ROOT = Path(__file__).resolve().parent.parent
DOCUMENT_LAYERS = {
    "ADOPTION_ADAPTER.md": "adapter",
    "AGENTS.md": "producer",
    "ENVIRONMENT_CONTRACT.md": "public-contract",
    "GITHUB_REPOSITORY_ADAPTER.md": "adapter",
    "PRODUCTION_STANDARD.md": "policy",
    "README.md": "navigation",
    "RELEASING.md": "producer",
    "REPOSITORY_GOVERNANCE.md": "public-contract",
    "SELF_HOSTING.md": "producer",
    "VERSIONING.md": "public-contract",
}
HIGH_LEVEL_PROCESS_DOCUMENTS = tuple(
    sorted(name for name, layer in DOCUMENT_LAYERS.items() if layer == "policy")
)
ADAPTER_CONTRACTS = {
    "ADOPTION_ADAPTER.md": "VERSIONING.md",
    "GITHUB_REPOSITORY_ADAPTER.md": "REPOSITORY_GOVERNANCE.md",
}
TEMPLATE_LAYERS = {
    "templates/AGENTS.process.md": "public-contract",
    "templates/PULL_REQUEST_TEMPLATE.md": "adapter",
}
ADDITIONAL_DOCUMENT_LAYERS = {
    ".github/PULL_REQUEST_TEMPLATE.md": "producer",
    "evals/README.md": "example",
}
ALLOWED_LAYER_REFERENCES = {
    "policy": {"policy", "public-contract"},
    "public-contract": {"policy", "public-contract"},
    "adapter": {"policy", "public-contract", "adapter"},
    "navigation": {*DOCUMENT_LAYERS.values(), "example"},
    "producer": {*DOCUMENT_LAYERS.values(), "example"},
    "example": {"policy", "public-contract"},
}
ABSTRACT_CONCEPT = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
MARKDOWN = MarkdownIt("commonmark", {"html": True})


def _root_document_target(source: Path, destination: str) -> str | None:
    parsed = urlsplit(destination)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    target = (source.parent / parsed.path).resolve()
    if target.parent != PROCESS_ROOT or target.suffix != ".md":
        return None
    return target.name


def _unlinked_inline_code(text: str) -> list[str]:
    code: list[str] = []
    for block in MARKDOWN.parse(text):
        if block.type != "inline" or not block.children:
            continue
        link_depth = 0
        for token in block.children:
            if token.type == "link_open":
                link_depth += 1
            elif token.type == "link_close":
                link_depth -= 1
            elif token.type == "code_inline" and link_depth == 0:
                code.append(token.content)
    return code


def _distributed_document_layer(path: Path) -> str | None:
    relative = path.relative_to(PROCESS_ROOT).as_posix()
    if path.parent == PROCESS_ROOT:
        return DOCUMENT_LAYERS.get(path.name)
    if relative in TEMPLATE_LAYERS:
        return TEMPLATE_LAYERS[relative]
    if relative in ADDITIONAL_DOCUMENT_LAYERS:
        return ADDITIONAL_DOCUMENT_LAYERS[relative]
    if relative.startswith("process_assets/skills/"):
        return "public-contract"
    return None


def _documentation_surfaces() -> list[Path]:
    return sorted(
        {
            *PROCESS_ROOT.glob("*.md"),
            *(PROCESS_ROOT / "templates").rglob("*.md"),
            *(PROCESS_ROOT / "process_assets" / "skills").rglob("*.md"),
            *(PROCESS_ROOT / ".github").rglob("*.md"),
            *(PROCESS_ROOT / "evals").rglob("*.md"),
        }
    )


def _registered_document_target(
    source: Path,
    destination: str,
    registry: dict[Path, str],
) -> Path | None:
    parsed = urlsplit(destination)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    target = (source.parent / parsed.path).resolve()
    try:
        target.relative_to(PROCESS_ROOT)
    except ValueError:
        return None
    if target.suffix != ".md":
        return None
    if target not in registry:
        raise AssertionError(f"unregistered Markdown reference target: {target}")
    return target


class DocumentationArchitectureTests(unittest.TestCase):
    def test_every_root_document_has_one_registered_layer(self):
        discovered = {path.name for path in PROCESS_ROOT.glob("*.md")}
        self.assertEqual(discovered, set(DOCUMENT_LAYERS))

    def test_every_distributed_markdown_surface_has_one_layer(self):
        for surface in _documentation_surfaces():
            with self.subTest(surface=str(surface)):
                self.assertIsNotNone(_distributed_document_layer(surface))

    def test_registered_document_references_follow_layer_direction(self):
        registry = {
            source.resolve(): _distributed_document_layer(source)
            for source in _documentation_surfaces()
        }
        self.assertTrue(all(layer is not None for layer in registry.values()))
        for source, raw_source_layer in registry.items():
            assert raw_source_layer is not None
            for _, destination in visible_markdown_links(
                source.read_text(encoding="utf-8")
            ):
                target = _registered_document_target(source, destination, registry)
                if target is None:
                    continue
                target_layer = registry[target]
                assert target_layer is not None
                with self.subTest(source=str(source), target=str(target)):
                    self.assertIn(
                        target_layer,
                        ALLOWED_LAYER_REFERENCES[raw_source_layer],
                    )

    def test_high_level_policy_has_abstract_document_shape(self):
        for relative in HIGH_LEVEL_PROCESS_DOCUMENTS:
            with self.subTest(document=relative):
                text = (PROCESS_ROOT / relative).read_text(encoding="utf-8")
                block_types = {token.type for token in MARKDOWN.parse(text)}
                self.assertTrue(
                    block_types.isdisjoint({"code_block", "fence", "html_block"})
                )
                for concept in _unlinked_inline_code(text):
                    self.assertRegex(concept, ABSTRACT_CONCEPT)

    def test_distributed_guidance_does_not_depend_on_adapter_or_producer_docs(self):
        skills_root = PROCESS_ROOT / "process_assets" / "skills"
        sources = [
            *(PROCESS_ROOT / "templates").rglob("*.md"),
            *skills_root.rglob("*.md"),
        ]
        for source in sorted(sources):
            for _, destination in visible_markdown_links(
                source.read_text(encoding="utf-8")
            ):
                target = _root_document_target(source, destination)
                if target is None:
                    continue
                with self.subTest(source=str(source), target=target):
                    self.assertIn(
                        DOCUMENT_LAYERS[target],
                        {"policy", "public-contract"},
                    )

    def test_producer_and_consumer_guidance_require_semantic_abstraction_review(self):
        producer = (PROCESS_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        consumer = (
            PROCESS_ROOT / "templates" / "AGENTS.process.md"
        ).read_text(encoding="utf-8")
        skill = (
            PROCESS_ROOT
            / "process_assets"
            / "skills"
            / "maintain-docs"
            / "SKILL.md"
        ).read_text(encoding="utf-8")

        self.assertIn("registered layers", producer)
        self.assertIn("independent semantic review", producer)
        self.assertIn("registered layers", consumer)
        self.assertIn("project owns its document registry", consumer.lower())
        self.assertIn("independent semantic review", skill)
        self.assertIn("Do not use a blacklist", skill)
        self.assertIn(
            "Treat product compatibility, deployment, migration, and retirement "
            "strategies as\n  consumer decisions",
            producer,
        )
        self.assertIn("must not infer a strategy", producer)
        self.assertIn(
            "Product compatibility, deployment, migration, and\n"
            "retirement strategies remain project decisions and are never inferred",
            consumer,
        )
        self.assertIn(
            "Do not infer a consumer compatibility, deployment, migration, or "
            "retirement\n  strategy",
            skill,
        )

    def test_provider_implementations_are_peer_adapter_documents(self):
        adapters = {
            name for name, layer in DOCUMENT_LAYERS.items() if layer == "adapter"
        }
        self.assertEqual(adapters, set(ADAPTER_CONTRACTS))
        for adapter in adapters:
            contract = ADAPTER_CONTRACTS[adapter]
            self.assertEqual("public-contract", DOCUMENT_LAYERS[contract])
            links = {
                _root_document_target(PROCESS_ROOT / adapter, destination)
                for _, destination in visible_markdown_links(
                    (PROCESS_ROOT / adapter).read_text(encoding="utf-8")
                )
            }
            self.assertIn(contract, links)

    def test_github_adapter_owns_remote_artifact_binding(self):
        adapter = (PROCESS_ROOT / "GITHUB_REPOSITORY_ADAPTER.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("immutable artifact ID", adapter)
        self.assertIn("service-computed artifact digest", adapter)
        self.assertIn("never replace the public N-1 lifecycle reports", adapter)

    def test_public_evidence_separates_semantics_from_machine_identity(self):
        standard = (PROCESS_ROOT / "PRODUCTION_STANDARD.md").read_text(
            encoding="utf-8"
        )
        skill = (
            PROCESS_ROOT
            / "process_assets"
            / "skills"
            / "publish-change"
            / "SKILL.md"
        ).read_text(encoding="utf-8")

        for raw_owner in (standard, skill):
            owner = " ".join(raw_owner.split())
            self.assertIn("independent role", owner)
            self.assertIn("cycle counters", owner)
            self.assertIn("machine-verifiable record", owner)
            self.assertIn("full checkpoint once", owner)
        self.assertIn(
            "audit fields, not public identities", " ".join(standard.split())
        )
        self.assertIn("omit its number from headings", " ".join(skill.split()))


if __name__ == "__main__":
    unittest.main()
