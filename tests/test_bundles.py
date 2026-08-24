import json
import tempfile
import unittest
from pathlib import Path

from engineering_process.bundles import load_bundles
from engineering_process.contracts import ContractError


PROCESS_ROOT = Path(__file__).resolve().parent.parent
SKILLS_ROOT = PROCESS_ROOT / "process_assets" / "skills"


class BundleTests(unittest.TestCase):
    def test_distribution_assigns_every_skill_once(self):
        bundles = load_bundles(PROCESS_ROOT, SKILLS_ROOT)

        self.assertEqual(
            bundles["core"],
            (
                "cross-repo-change",
                "define-change-contract",
                "evolve-process",
                "finish-change",
                "implement-change",
                "plan-change",
                "publish-change",
                "review-change",
                "run-change",
                "verify-change",
            ),
        )
        self.assertEqual(
            bundles["delivery"],
            ("assess-design", "run-project-command"),
        )
        self.assertEqual(
            bundles["product"],
            ("implement-use-case", "specify-use-case"),
        )
        self.assertEqual(
            bundles["architecture"],
            ("design-module", "implement-module"),
        )
        self.assertEqual(bundles["api"], ("change-api",))
        self.assertEqual(
            bundles["frontend"],
            ("build-frontend", "build-frontend-foundation", "govern-ui"),
        )
        self.assertEqual(bundles["mcp"], ("integrate-mcp",))
        self.assertEqual(bundles["docs"], ("maintain-docs",))
        self.assertNotIn("publication", bundles)

    def test_rejects_duplicate_bundle_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bundles.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "bundles": {
                            "one": ["verify-change"],
                            "two": ["verify-change"],
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ContractError, "belongs to both"):
                load_bundles(root, SKILLS_ROOT)
