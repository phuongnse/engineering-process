import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engineering_process.contracts import ContractError, validate_plan, validate_release
from engineering_process.publication import validate_pull_request
from engineering_process.release_candidate import (
    prepare_release_candidate,
    render_release_pull_request,
)
from verification.prepare_release_review import approved_review_from_assignment


class ReleaseCandidateTests(unittest.TestCase):
    def test_release_review_preserves_the_immutable_assignment(self):
        assignment = {
            "changeId": "release-0-2-0",
            "cycle": 1,
            "checkpoint": "a" * 40,
            "workspaceFingerprint": f"sha256:{'b' * 64}",
            "comparisonBase": "c" * 40,
            "reviewer": {
                "actorId": "github-release-reviewer",
                "contextId": "release-pr-7-" + "a" * 40,
                "kind": "human",
            },
            "independence": {
                "method": "separate-person",
                "attestedBy": "github-repository-rules",
                "evidence": "github://sample/repository/pull/7",
            },
        }

        report = approved_review_from_assignment(assignment)

        self.assertEqual("approved", report["verdict"])
        self.assertEqual(assignment["checkpoint"], report["checkpoint"])
        self.assertEqual(assignment["reviewer"], report["reviewer"])

    def initialize_project(self, root: Path) -> None:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "release-test@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Release Test"], cwd=root, check=True
        )
        (root / "engineering_process").mkdir()
        (root / ".process").mkdir()
        (root / "release-changes").mkdir()
        (root / "release-changes" / "README.md").write_text(
            "release changes\n", encoding="utf-8"
        )
        (root / "pyproject.toml").write_text(
            '[project]\nname = "sample"\nversion = "0.1.1"\n',
            encoding="utf-8",
        )
        (root / "engineering_process" / "__init__.py").write_text(
            'VERSION = "0.1.1"\n', encoding="utf-8"
        )
        (root / ".process" / "process.lock").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "process": {
                        "version": "0.1.1",
                        "digest": f"sha256:{'0' * 64}",
                    },
                    "skills": ["run-change"],
                }
            ),
            encoding="utf-8",
        )
        (root / "release.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "previousVersion": "0.1.0",
                    "version": "0.1.1",
                    "classification": "patch",
                    "compatibility": "backward-compatible",
                    "schemaImpact": "unchanged",
                    "migration": None,
                    "identity": {
                        "package": "sample",
                        "distribution": "sample",
                        "tag": "v0.1.1",
                        "releaseName": "sample 0.1.1",
                        "runtimeVersion": {
                            "path": "engineering_process/__init__.py",
                            "variable": "VERSION",
                        },
                        "artifacts": [
                            "sample-0.1.1-py3-none-any.whl",
                            "sample-0.1.1.tar.gz",
                        ],
                        "receiptAsset": None,
                    },
                    "provenance": {
                        "mode": "bootstrap-history",
                        "statement": "Recorded bootstrap history.",
                        "lifecycleReceipt": None,
                    },
                    "changes": [
                        {
                            "id": "historical-fix",
                            "type": "fix",
                            "surfaces": ["runtime"],
                            "rationale": "Record the historical fix.",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "chore: initialize release history"],
            cwd=root,
            check=True,
        )
        subprocess.run(["git", "tag", "v0.1.1"], cwd=root, check=True)

    def write_change(self, root: Path) -> None:
        (root / "release-changes" / "automated-release.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "id": "automated-release",
                    "type": "capability",
                    "surfaces": ["publication", "workflow"],
                    "rationale": "Automate reviewed Release PR publication.",
                    "schemaImpact": "additive",
                    "migration": None,
                }
            ),
            encoding="utf-8",
        )

    def test_materializes_bootstrap_release_and_every_declared_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.initialize_project(root)
            self.write_change(root)
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import engineering_process; assert engineering_process.VERSION == '0.1.1'",
                ],
                cwd=root,
                check=True,
            )

            result = prepare_release_candidate(root)

            self.assertEqual("0.2.0", result["version"])
            self.assertEqual("bootstrap-authority", result["provenanceMode"])
            release = json.loads((root / "release.json").read_text(encoding="utf-8"))
            validated = validate_release(release)
            self.assertEqual("0.2.0", validated.version)
            self.assertEqual(
                "sample-v0.2.0-bootstrap-authorization.json",
                validated.authorization_asset,
            )
            self.assertEqual(
                '[project]\nname = "sample"\nversion = "0.2.0"\n',
                (root / "pyproject.toml").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                'VERSION = "0.2.0"\n',
                (root / "engineering_process" / "__init__.py").read_text(
                    encoding="utf-8"
                ),
            )
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import engineering_process; assert engineering_process.VERSION == '0.2.0'",
                ],
                cwd=root,
                check=True,
            )
            self.assertFalse(
                (root / "release-changes" / "automated-release.json").exists()
            )
            change = json.loads(
                (root / ".release" / "change.json").read_text(encoding="utf-8")
            )
            plan = json.loads(
                (root / ".release" / "plan.json").read_text(encoding="utf-8")
            )
            validate_plan(plan)
            self.assertEqual("release-0-2-0", change["id"])
            contract_bytes = (root / ".release" / "change.json").read_bytes()
            self.assertEqual(
                "sha256:" + hashlib.sha256(contract_bytes).hexdigest(),
                plan["contractDigest"],
            )

    def test_requires_self_adoption_before_the_release_after_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.initialize_project(root)
            self.write_change(root)
            prepare_release_candidate(root)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "chore: prepare bootstrap authority"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "tag", "v0.2.0"], cwd=root, check=True)
            self.write_change(root)

            with self.assertRaisesRegex(ContractError, "self-adoption"):
                prepare_release_candidate(root)

    def test_rejects_a_release_change_directory_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            self.initialize_project(root)
            self.write_change(root)
            alias = Path(directory) / "changes-alias"
            try:
                alias.symlink_to(root / "release-changes", target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlink unavailable: {error}")

            with self.assertRaisesRegex(ContractError, "must not be a symlink"):
                prepare_release_candidate(root, changes_dir=alias)

    def test_renders_valid_draft_and_approved_release_pr_bodies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.initialize_project(root)
            self.write_change(root)
            prepare_release_candidate(root)

            draft = render_release_pull_request(root, approved=False)
            approved = render_release_pull_request(root, approved=True)

            self.assertEqual(
                [],
                validate_pull_request(
                    title="chore(release): prepare v0.2.0",
                    body=draft,
                    branch="automation/release/next",
                    state="draft",
                ),
            )
            self.assertEqual(
                [],
                validate_pull_request(
                    title="chore(release): prepare v0.2.0",
                    body=approved,
                    branch="automation/release/next",
                    state="ready",
                ),
            )


if __name__ == "__main__":
    unittest.main()
