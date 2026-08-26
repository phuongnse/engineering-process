import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engineering_process.contracts import (
    ContractError,
    validate_change,
    validate_plan,
    validate_release,
    validate_release_change,
)
from engineering_process.evidence import (
    _plan_decision_entries,
    _validate_plan_decision_receipt,
)
from engineering_process.publication import validate_pull_request
from engineering_process.release_candidate import (
    _resolved_improvement_catalog,
    prepare_release_candidate,
    render_release_pull_request,
    validate_generated_release_lifecycle_plan,
)
class ReleaseCandidateTests(unittest.TestCase):
    def enable_plan_decision_policy(self, root: Path) -> None:
        project = {
            "schemaVersion": 3,
            "project": "sample",
            "lifecycle": {
                "requiredProfiles": ["development", "review"],
                "planDecision": {
                    "mode": "provenance-gated-authored-review",
                    "materialCategories": [
                        "architecture",
                        "authority",
                        "compatibility",
                        "external-mutation",
                        "lifecycle-order",
                        "owner",
                        "rollout",
                        "scope",
                        "trust-boundary",
                    ],
                },
            },
            "profiles": {
                "development": [{
                    "id": "unit",
                    "run": ["python", "-c", "raise SystemExit(0)"],
                    "timeoutSeconds": 10,
                }],
                "review": [{
                    "id": "review",
                    "run": ["python", "-c", "raise SystemExit(0)"],
                    "timeoutSeconds": 10,
                }],
            },
            "environment": {
                "defaultProfile": "development",
                "foregroundOnly": True,
                "managedTools": [],
                "profiles": {
                    "development": ["python-runtime"],
                    "review": ["python-runtime"],
                },
                "requirements": [{
                    "id": "python-runtime",
                    "description": "Python runtime",
                    "probe": {
                        "run": ["python", "--version"],
                        "timeoutSeconds": 10,
                        "readOnly": True,
                        "outputStream": "combined",
                        "outputRegex": "^Python 3\\.",
                    },
                    "remediation": "Install Python.",
                }],
                "setupActions": [],
            },
        }
        (root / ".process" / "project.json").write_text(
            json.dumps(project) + "\n", encoding="utf-8"
        )
        subprocess.run(["git", "add", ".process/project.json"], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "chore: adopt plan decision policy"],
            cwd=root,
            check=True,
        )

    def assert_federated_migration_contract(
        self, aggregate: str, federated_migration: str | None
    ) -> None:
        self.assertLessEqual(len(aggregate), 1_000)
        if federated_migration is None:
            return
        for required in (
            "Must adopt cross-repo-change",
            "classify governed failures/findings",
            "consumers await disposition",
            "immutable release",
            "exact reproduction",
            "Released lifecycle/evidence readers retain historical meaning",
        ):
            self.assertIn(required, federated_migration)

    def test_source_release_migration_aggregate_fits_public_contract(self):
        changes_dir = Path(__file__).resolve().parent.parent / "release-changes"
        if not changes_dir.is_dir():
            self.skipTest("source release-change fragments are not packaged")
        migrations = []
        federated_migration = None
        for path in sorted(changes_dir.glob("*.json")):
            change = validate_release_change(
                json.loads(path.read_text(encoding="utf-8")), str(path)
            )
            if change.migration is None:
                continue
            migrations.append(f"{change.identifier}: {change.migration}")
            if change.identifier == "federated-process-improvement":
                federated_migration = change.migration

        if federated_migration is None:
            release = validate_release(
                json.loads(
                    (changes_dir.parent / "release.json").read_text(
                        encoding="utf-8"
                    )
                )
            )
            aggregate = release.migration or ""
            if "federated-process-improvement:" in aggregate:
                federated_migration = aggregate
        else:
            aggregate = "; ".join(migrations)
        self.assert_federated_migration_contract(aggregate, federated_migration)

    def test_migration_free_patch_has_no_legacy_guidance_requirement(self):
        self.assert_federated_migration_contract("", None)

    def test_generated_release_closes_only_catalog_entries_in_its_change_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = {
                "schemaVersion": 1,
                "kind": "engineering-process-improvement-catalog",
                "producer": {
                    "project": "sample",
                    "repository": "example/sample",
                },
                "entries": [
                    {
                        "id": "first-invariant",
                        "reusableClass": "deterministic-enforcement",
                        "status": "active",
                        "publicSurfaces": ["lifecycle"],
                        "lastResolution": None,
                        "activeChangeId": "included-change",
                    },
                    {
                        "id": "second-invariant",
                        "reusableClass": "portability-gap",
                        "status": "active",
                        "publicSurfaces": ["verification"],
                        "lastResolution": None,
                        "activeChangeId": "later-change",
                    },
                ],
            }
            (root / "improvement-catalog.json").write_text(
                json.dumps(catalog) + "\n", encoding="utf-8"
            )

            result = _resolved_improvement_catalog(
                root, change_ids={"included-change"}, version="0.2.0"
            )

            self.assertIsNotNone(result)
            assert result is not None
            _path, content = result
            updated = json.loads(content)
            self.assertEqual("resolved", updated["entries"][0]["status"])
            self.assertEqual(
                {"changeId": "included-change", "version": "0.2.0"},
                updated["entries"][0]["lastResolution"],
            )
            self.assertIsNone(updated["entries"][0]["activeChangeId"])
            self.assertEqual("active", updated["entries"][1]["status"])

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
            validate_change(change)
            validate_plan(plan)
            self.assertEqual("release-0-2-0", change["id"])
            self.assertEqual(3, change["schemaVersion"])
            self.assertEqual(2, plan["schemaVersion"])
            self.assertEqual(
                [
                    ".release/change.json",
                    ".release/plan.json",
                    "engineering_process/__init__.py",
                    "improvement-catalog.json",
                    "pyproject.toml",
                    "release-changes/",
                    "release.json",
                ],
                plan["workItems"][0]["affectedPaths"],
            )
            assessments = change["quality"]["assessments"]
            self.assertEqual(
                [
                    "compatibility",
                    "correctness",
                    "maintainability",
                    "observability",
                    "operability",
                    "performance",
                    "privacy",
                    "reliability",
                    "security",
                    "supply-chain",
                ],
                [assessment["dimension"] for assessment in assessments],
            )
            self.assertEqual(
                {"performance", "privacy"},
                {
                    assessment["dimension"]
                    for assessment in assessments
                    if assessment["status"] == "not-applicable"
                },
            )
            contract_bytes = (root / ".release" / "change.json").read_bytes()
            self.assertEqual(
                "sha256:" + hashlib.sha256(contract_bytes).hexdigest(),
                plan["contractDigest"],
            )

    def test_adopted_policy_emits_and_exactly_recomputes_release_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.initialize_project(root)
            self.enable_plan_decision_policy(root)
            self.write_change(root)

            prepare_release_candidate(root)

            contract = json.loads(
                (root / ".release" / "change.json").read_text(encoding="utf-8")
            )
            plan = json.loads(
                (root / ".release" / "plan.json").read_text(encoding="utf-8")
            )
            self.assertEqual(3, plan["schemaVersion"])
            self.assertEqual("process-generated", plan["provenance"]["kind"])
            validate_generated_release_lifecycle_plan(
                root, project="sample", contract=contract, plan=plan
            )

            for relative in (
                ".process/project.json",
                ".release/change.json",
                "release.json",
            ):
                path = root / relative
                original = path.read_bytes()
                for malformed in (b"{", b"\xff"):
                    with self.subTest(relative=relative, malformed=malformed):
                        path.write_bytes(malformed)
                        with self.assertRaisesRegex(
                            ContractError, "invalid UTF-8 JSON"
                        ):
                            validate_generated_release_lifecycle_plan(
                                root,
                                project="sample",
                                contract=contract,
                                plan=plan,
                            )
                path.write_bytes(original)

            plan["provenance"]["generator"] = "claimed-generator"
            with self.assertRaisesRegex(ContractError, "unknown.*generator"):
                validate_generated_release_lifecycle_plan(
                    root, project="sample", contract=contract, plan=plan
                )

    def test_portable_generated_plan_evidence_recomputes_exact_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.initialize_project(root)
            self.enable_plan_decision_policy(root)
            self.write_change(root)
            prepare_release_candidate(root)
            contract = json.loads(
                (root / ".release" / "change.json").read_text(encoding="utf-8")
            )
            plan = json.loads(
                (root / ".release" / "plan.json").read_text(encoding="utf-8")
            )

            decision_state = {
                "kind": "process-generated",
                "authorized": True,
                "assignment": None,
                "review": None,
                "recommendation": None,
                "recommendationAssignment": None,
                "recommendationReview": None,
                "resolution": None,
            }
            process = plan["provenance"]["authority"]
            state = {
                "project": "sample",
                "planDecision": decision_state,
                "implementationActors": [],
                "plan": {
                    "path": ".release/plan.json",
                    "digest": (
                        "sha256:"
                        + hashlib.sha256(
                            (root / ".release" / "plan.json").read_bytes()
                        ).hexdigest()
                    ),
                },
            }
            exported = _plan_decision_entries(root, state)
            self.assertIsNotNone(exported)
            assert exported is not None
            self.assertEqual(3, len(exported["generatedInputs"]))
            _validate_plan_decision_receipt(
                exported,
                state=state,
                contract=contract,
                plan=plan,
                process=process,
            )

            for mutation, message in (
                (("generator", "unregistered-generator"), "unknown.*generator"),
                (("authority", {"version": "9.9.9", "digest": f"sha256:{'9' * 64}"}), "exact core recomputation"),
            ):
                tampered = json.loads(json.dumps(plan))
                tampered["provenance"][mutation[0]] = mutation[1]
                with self.subTest(mutation=mutation[0]), self.assertRaisesRegex(
                    ContractError, message
                ):
                    _validate_plan_decision_receipt(
                        exported,
                        state=state,
                        contract=contract,
                        plan=tampered,
                        process=process,
                    )

            tampered = json.loads(json.dumps(plan))
            tampered["provenance"]["inputs"][0]["sha256"] = f"sha256:{'8' * 64}"
            with self.assertRaisesRegex(ContractError, "do not match plan provenance"):
                _validate_plan_decision_receipt(
                    exported,
                    state=state,
                    contract=contract,
                    plan=tampered,
                    process=process,
                )

            plan = json.loads(
                (root / ".release" / "plan.json").read_text(encoding="utf-8")
            )
            plan["approach"] = "A claimed generated plan with altered content."
            with self.assertRaisesRegex(ContractError, "exact core recomputation"):
                validate_generated_release_lifecycle_plan(
                    root, project="sample", contract=contract, plan=plan
                )

    def test_materialization_preserves_crlf_runtime_version_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.initialize_project(root)
            self.write_change(root)
            runtime_path = root / "engineering_process" / "__init__.py"
            runtime_path.write_bytes(b'VERSION = "0.1.1"\r\n')
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "test: use CRLF runtime source"],
                cwd=root,
                check=True,
            )

            prepare_release_candidate(root)

            self.assertEqual(b'VERSION = "0.2.0"\r\n', runtime_path.read_bytes())

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
