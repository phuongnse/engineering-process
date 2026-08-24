import base64
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from engineering_process.contracts import (
    ContractError,
    canonical_json_digest,
    validate_automation_proposal,
)
from engineering_process.publication import (
    PR_DESCRIPTION_END,
    PR_DESCRIPTION_START,
    validate_branch,
    validate_commit_range,
    validate_commit_subject,
    validate_completed_publication,
    validate_controlled_automation_proposal,
    validate_controlled_automation_proposal_completion,
    validate_pr_title,
    validate_pull_request,
    validate_evidence_publication,
)


def pr_body(status: str = "satisfied") -> str:
    checked = "x" if status != "pending" else " "
    return f"""{PR_DESCRIPTION_START}
## Summary

Adopt the shared publication contract.

## Contract and scope

change-contract: publication-standard

## Impact and risk

No runtime behavior changes; publication metadata becomes portable.

## Verification

`python -m unittest`

## Independent review

Separate reviewer approved checkpoint abc123.

## Requirements and rules followed

- [{checked}] **Scope and contract** — accepted scope is implemented without unapproved expansion. [status: {status}]
- [{checked}] **Verification evidence** — required current profiles pass on the published checkpoint. [status: {status}]
- [{checked}] **Independent review** — a separate reviewer approved the published checkpoint with no open required finding. [status: {status}]
{PR_DESCRIPTION_END}
"""


class PublicationTests(unittest.TestCase):
    def controlled_proposal_fixture(self, root: Path, *, status: str = "pending"):
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "process-test@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Process Test"], cwd=root, check=True
        )
        example = json.loads(
            (
                Path(__file__).resolve().parent.parent
                / "examples"
                / "automation-proposal.json"
            ).read_text(encoding="utf-8")
        )
        policy = example["optIn"]["document"]
        policy_path = root / ".process" / "automation-proposals.json"
        policy_path.parent.mkdir()
        policy_path.write_text(
            json.dumps(policy, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (root / "package.json").write_text('{"dependencies":{"sample":"1.0.0"}}\n')
        (root / "package-lock.json").write_text('{"sample":"1.0.0"}\n')
        (root / "SECURITY.md").write_text("protected policy\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "chore: initialize proposal fixture"],
            cwd=root,
            check=True,
        )
        base_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
        subprocess.run(
            ["git", "switch", "-qc", "automation/renovate/update-example"],
            cwd=root,
            check=True,
        )
        (root / "package.json").write_text('{"dependencies":{"sample":"2.0.0"}}\n')
        (root / "package-lock.json").write_text('{"sample":"2.0.0"}\n')
        subprocess.run(["git", "add", "package.json", "package-lock.json"], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "chore(deps): update sample dependency"],
            cwd=root,
            check=True,
        )
        head_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
        body = pr_body(status)
        title = "chore(deps): update example dependency"
        example.update(
            {
                "baseSha": base_sha,
                "headSha": head_sha,
                "title": title,
                "bodySha256": "sha256:"
                + hashlib.sha256(body.encode("utf-8")).hexdigest(),
            }
        )
        example["optIn"]["sha256"] = canonical_json_digest(policy)
        return validate_automation_proposal(example), body, title, head_sha

    def test_controlled_proposal_is_untrusted_but_exactly_policy_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proposal, body, title, head_sha = self.controlled_proposal_fixture(root)
            source = {
                "dirty": False,
                "checkpoint": head_sha,
                "fingerprint": f"sha256:{'a' * 64}",
            }

            self.assertEqual(
                [],
                validate_controlled_automation_proposal(
                    root,
                    repository="example/project",
                    title=title,
                    body=body,
                    branch="automation/renovate/update-example",
                    target_branch="main",
                    base_commit=proposal.base_sha,
                    state="draft",
                    commit=head_sha,
                    verifier_repository="example/policy-verifier",
                    verifier_commit="4" * 40,
                    proposal=proposal,
                    source=source,
                ),
            )
            issues = validate_controlled_automation_proposal(
                root,
                repository="example/project",
                title=title,
                body=body,
                branch="automation/renovate/update-example",
                target_branch="main",
                base_commit=proposal.base_sha,
                state="draft",
                commit=head_sha,
                verifier_repository="example/different-verifier",
                verifier_commit="4" * 40,
                proposal=proposal,
                source=source,
            )

            self.assertTrue(any("verifier repository" in issue for issue in issues))

            policy_path = root / ".process" / "automation-proposals.json"
            policy_path.write_text(
                policy_path.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", ".process/automation-proposals.json"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-qm", "chore: mutate proposal policy"],
                cwd=root,
                check=True,
            )
            protected_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            protected = replace(
                proposal,
                head_sha=protected_head,
                changed_paths=(
                    ".process/automation-proposals.json",
                    "package-lock.json",
                    "package.json",
                ),
            )
            protected_issues = validate_controlled_automation_proposal(
                root,
                repository="example/project",
                title=title,
                body=body,
                branch="automation/renovate/update-example",
                target_branch="main",
                base_commit=protected.base_sha,
                state="draft",
                commit=protected_head,
                verifier_repository="example/policy-verifier",
                verifier_commit="4" * 40,
                proposal=protected,
                source={
                    "dirty": False,
                    "checkpoint": protected_head,
                    "fingerprint": f"sha256:{'a' * 64}",
                },
            )

            self.assertTrue(
                any("cannot change process" in issue for issue in protected_issues)
            )

    def test_controlled_proposal_rejects_stale_actual_protected_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proposal, body, title, head_sha = self.controlled_proposal_fixture(root)
            subprocess.run(["git", "switch", "-q", "main"], cwd=root, check=True)
            (root / "README.md").write_text("base advanced\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "docs: advance protected base"],
                cwd=root,
                check=True,
            )
            actual_base = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            subprocess.run(
                ["git", "switch", "-q", "automation/renovate/update-example"],
                cwd=root,
                check=True,
            )

            issues = validate_controlled_automation_proposal(
                root,
                repository="example/project",
                title=title,
                body=body,
                branch="automation/renovate/update-example",
                target_branch="main",
                base_commit=actual_base,
                state="draft",
                commit=head_sha,
                verifier_repository="example/policy-verifier",
                verifier_commit="4" * 40,
                proposal=proposal,
                source={
                    "dirty": False,
                    "checkpoint": head_sha,
                    "fingerprint": f"sha256:{'a' * 64}",
                },
            )

            self.assertTrue(any("base SHA" in issue for issue in issues))

    def test_controlled_proposal_checks_both_sides_of_protected_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proposal, body, title, _head_sha = self.controlled_proposal_fixture(root)
            subprocess.run(
                ["git", "mv", "SECURITY.md", "SECURITY-old.md"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-qm", "chore: rename protected policy"],
                cwd=root,
                check=True,
            )
            head_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            renamed = replace(
                proposal,
                head_sha=head_sha,
                changed_paths=(
                    "SECURITY-old.md",
                    "SECURITY.md",
                    "package-lock.json",
                    "package.json",
                ),
            )
            issues = validate_controlled_automation_proposal(
                root,
                repository="example/project",
                title=title,
                body=body,
                branch="automation/renovate/update-example",
                target_branch="main",
                base_commit=proposal.base_sha,
                state="draft",
                commit=head_sha,
                verifier_repository="example/policy-verifier",
                verifier_commit="4" * 40,
                proposal=renamed,
                source={
                    "dirty": False,
                    "checkpoint": head_sha,
                    "fingerprint": f"sha256:{'a' * 64}",
                },
            )

            self.assertTrue(any("SECURITY.md" in issue for issue in issues))

    @unittest.skipIf(os.name == "nt", "Git symlink type changes require POSIX")
    def test_controlled_proposal_checks_protected_git_type_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proposal, body, title, _head_sha = self.controlled_proposal_fixture(root)
            security = root / "SECURITY.md"
            security.unlink()
            security.symlink_to("package.json")
            subprocess.run(["git", "add", "SECURITY.md"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "chore: change protected policy type"],
                cwd=root,
                check=True,
            )
            head_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            type_changed = replace(
                proposal,
                head_sha=head_sha,
                changed_paths=(
                    "SECURITY.md",
                    "package-lock.json",
                    "package.json",
                ),
            )
            issues = validate_controlled_automation_proposal(
                root,
                repository="example/project",
                title=title,
                body=body,
                branch="automation/renovate/update-example",
                target_branch="main",
                base_commit=proposal.base_sha,
                state="draft",
                commit=head_sha,
                verifier_repository="example/policy-verifier",
                verifier_commit="4" * 40,
                proposal=type_changed,
                source={
                    "dirty": False,
                    "checkpoint": head_sha,
                    "fingerprint": f"sha256:{'a' * 64}",
                },
            )

            self.assertTrue(any("SECURITY.md" in issue for issue in issues))

    def test_controlled_proposal_requires_opt_in_from_the_exact_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proposal, body, title, head_sha = self.controlled_proposal_fixture(root)
            subprocess.run(
                ["git", "rm", "-q", ".process/automation-proposals.json"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-qm", "chore: remove proposal policy"],
                cwd=root,
                check=True,
            )
            base_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            (root / "package.json").write_text(
                '{"dependencies":{"sample":"3.0.0"}}\n'
            )
            (root / "package-lock.json").write_text('{"sample":"3.0.0"}\n')
            subprocess.run(
                ["git", "add", "package.json", "package-lock.json"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-qm", "chore(deps): update sample again"],
                cwd=root,
                check=True,
            )
            head_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            proposal = replace(
                proposal,
                base_sha=base_sha,
                head_sha=head_sha,
            )
            with self.assertRaisesRegex(ContractError, "opt-in policy on the base"):
                validate_controlled_automation_proposal(
                    root,
                    repository="example/project",
                    title=title,
                    body=body,
                    branch="automation/renovate/update-example",
                    target_branch="main",
                    base_commit=proposal.base_sha,
                    state="draft",
                    commit=head_sha,
                    verifier_repository="example/policy-verifier",
                    verifier_commit="4" * 40,
                    proposal=proposal,
                    source={
                        "dirty": False,
                        "checkpoint": head_sha,
                        "fingerprint": f"sha256:{'a' * 64}",
                    },
                )

    def test_controlled_proposal_completion_requires_exact_ready_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proposal, body, title, head_sha = self.controlled_proposal_fixture(
                root, status="satisfied"
            )
            fingerprint = f"sha256:{'a' * 64}"
            source = {
                "dirty": False,
                "checkpoint": head_sha,
                "fingerprint": fingerprint,
            }
            receipt = {
                "project": "sample-project",
                "checkpoint": head_sha,
                "comparisonBase": proposal.base_sha,
                "workspaceFingerprint": fingerprint,
            }
            arguments = {
                "repository": "example/project",
                "project": "sample-project",
                "title": title,
                "body": body,
                "branch": "automation/renovate/update-example",
                "target_branch": "main",
                "base_commit": proposal.base_sha,
                "commit": head_sha,
                "verifier_repository": "example/policy-verifier",
                "verifier_commit": "4" * 40,
                "proposal": proposal,
                "source": source,
            }

            self.assertEqual(
                [],
                validate_controlled_automation_proposal_completion(
                    root,
                    evidence=receipt,
                    **arguments,
                ),
            )
            pending_body = pr_body("pending")
            pending = replace(
                proposal,
                body_sha256="sha256:"
                + hashlib.sha256(pending_body.encode("utf-8")).hexdigest(),
            )
            pending_issues = validate_controlled_automation_proposal_completion(
                root,
                evidence=receipt,
                **{
                    **arguments,
                    "body": pending_body,
                    "proposal": pending,
                },
            )
            self.assertTrue(
                any("not ready for publication" in issue for issue in pending_issues)
            )
            receipt["checkpoint"] = "f" * 40
            issues = validate_controlled_automation_proposal_completion(
                root,
                evidence=receipt,
                **arguments,
            )

            self.assertTrue(
                any("publication commit" in issue.lower() for issue in issues)
            )
            receipt["checkpoint"] = head_sha
            receipt["comparisonBase"] = "f" * 40
            issues = validate_controlled_automation_proposal_completion(
                root,
                evidence=receipt,
                **arguments,
            )
            self.assertTrue(
                any("comparison base" in issue.lower() for issue in issues)
            )

    def test_completed_publication_binds_exact_lifecycle_checkpoint(self):
        checkpoint = "a" * 40
        fingerprint = f"sha256:{'b' * 64}"
        lifecycle = {
            "phase": "completed",
            "completion": {"path": "completion.json"},
            "current": True,
            "pendingFindings": [],
            "verification": [
                {
                    "checkpoint": checkpoint,
                    "workspaceFingerprint": fingerprint,
                }
            ],
        }
        source = {
            "dirty": False,
            "checkpoint": checkpoint,
            "fingerprint": fingerprint,
        }

        self.assertEqual(
            [],
            validate_completed_publication(
                title="feat(process): standardize publication",
                body=pr_body(),
                branch="feat/standardize-publication",
                commit=checkpoint,
                lifecycle=lifecycle,
                source=source,
            ),
        )

    def test_publication_rejects_verified_or_stale_source(self):
        checkpoint = "a" * 40
        fingerprint = f"sha256:{'b' * 64}"
        lifecycle = {
            "phase": "verified",
            "completion": None,
            "current": True,
            "pendingFindings": [],
            "verification": [
                {
                    "checkpoint": checkpoint,
                    "workspaceFingerprint": fingerprint,
                }
            ],
        }
        source = {
            "dirty": True,
            "checkpoint": checkpoint,
            "fingerprint": fingerprint,
        }

        issues = validate_completed_publication(
            title="feat(process): standardize publication",
            body=pr_body("pending"),
            branch="feat/standardize-publication",
            commit="c" * 40,
            lifecycle=lifecycle,
            source=source,
        )

        self.assertTrue(any("completed lifecycle" in issue for issue in issues))
        self.assertTrue(any("clean working tree" in issue for issue in issues))
        self.assertTrue(any("current source checkpoint" in issue for issue in issues))
        self.assertTrue(any("not ready for publication" in issue for issue in issues))

    def test_receipt_publication_binds_project_checkpoint_and_workspace(self):
        checkpoint = "a" * 40
        fingerprint = f"sha256:{'b' * 64}"
        receipt = {
            "project": "sample",
            "checkpoint": checkpoint,
            "workspaceFingerprint": fingerprint,
        }
        source = {
            "dirty": False,
            "checkpoint": checkpoint,
            "fingerprint": fingerprint,
        }

        self.assertEqual(
            [],
            validate_evidence_publication(
                title="feat(process): standardize publication",
                body=pr_body(),
                branch="feat/standardize-publication",
                commit=checkpoint,
                project="sample",
                evidence=receipt,
                source=source,
            ),
        )

        issues = validate_evidence_publication(
            title="feat(process): standardize publication",
            body=pr_body(),
            branch="feat/standardize-publication",
            commit=checkpoint,
            project="other",
            evidence=dict(receipt, workspaceFingerprint=f"sha256:{'c' * 64}"),
            source=source,
        )
        self.assertTrue(any("publication project" in issue for issue in issues))
        self.assertTrue(any("publication workspace" in issue for issue in issues))

    def test_accepts_manual_and_generic_automation_branches(self):
        self.assertEqual([], validate_branch("feat/add-workspace"))
        self.assertEqual([], validate_branch("automation/renovate/runtime-packages"))
        self.assertTrue(validate_branch("renovate/runtime-packages"))
        self.assertTrue(validate_branch("agent/add-workspace"))

    def test_enforces_conventional_commit_and_pr_subjects(self):
        self.assertEqual([], validate_commit_subject("fix: reject stale evidence"))
        self.assertEqual([], validate_pr_title("feat(process): standardize publication"))
        self.assertTrue(validate_commit_subject("Reject stale evidence"))
        self.assertTrue(validate_pr_title("fix: sentence ends with period."))

    def test_ready_pr_requires_resolved_standard_requirements(self):
        issues = validate_pull_request(
            title="feat(process): standardize publication",
            body=pr_body("pending"),
            branch="feat/standardize-publication",
            state="ready",
        )

        self.assertTrue(any("not ready for publication" in issue for issue in issues))

    def test_draft_pr_allows_pending_standard_requirements(self):
        self.assertEqual(
            [],
            validate_pull_request(
                title="feat(process): standardize publication",
                body=pr_body("pending"),
                branch="feat/standardize-publication",
                state="draft",
            ),
        )

    def test_ready_pr_accepts_complete_body_and_project_requirements(self):
        body = pr_body() + (
            "\n## Project-specific requirements\n\n"
            "- [x] **Project-specific: UI evidence** — no UI surface changed. "
            "[reason: process-only change] [status: not-applicable]\n"
        )
        self.assertEqual(
            [],
            validate_pull_request(
                title="feat(process): standardize publication",
                body=body,
                branch="feat/standardize-publication",
                state="ready",
            ),
        )

    def test_draft_renovate_pr_accepts_only_bounded_debug_metadata(self):
        payload = base64.b64encode(
            json.dumps(
                {
                    "createdInVer": "44.37.1",
                    "updatedInVer": "44.37.1",
                    "targetBranch": "main",
                    "labels": [],
                },
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")
        body = pr_body("pending") + f"\n<!--renovate-debug:{payload}-->\n"

        self.assertEqual(
            [],
            validate_pull_request(
                title="chore(deps): update actions/checkout action to v7",
                body=body,
                branch="automation/renovate/actions-checkout-7.x",
                state="draft",
            ),
        )

    def test_renovate_metadata_does_not_allow_arbitrary_html_or_contracts(self):
        valid = {
            "createdInVer": "44.37.1",
            "updatedInVer": "44.37.1",
            "targetBranch": "main",
            "labels": [],
        }
        unexpected = {**valid, "extra": "not-allowed"}
        encoded = base64.b64encode(
            json.dumps(unexpected, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        bodies = (
            pr_body("pending") + "\n<div>hidden policy</div>\n",
            pr_body("pending") + "\n<!--renovate-debug:not-base64-->\n",
            pr_body("pending") + f"\n<!--renovate-debug:{encoded}-->\n",
        )

        for body in bodies:
            with self.subTest(body=body[-100:]):
                self.assertTrue(
                    validate_pull_request(
                        title="chore(deps): update actions/checkout action to v7",
                        body=body,
                        branch="automation/renovate/actions-checkout-7.x",
                        state="draft",
                    )
                )
    def test_project_extensions_cannot_redefine_common_policy(self):
        duplicate_section = pr_body() + (
            "\n## Independent review\n\nIndependent review is not required here.\n"
        )
        duplicate_requirement = pr_body() + (
            "\n- [x] **Independent review** — project-local review is optional. "
            "[status: satisfied]\n"
        )
        shadowing_detail = pr_body() + (
            "\n## Project-specific requirements\n\n"
            "- [x] **Project-specific: Reviewer approval** — Independent review "
            "is optional for this project. [status: satisfied]\n"
        )
        rendered_shadowing_details = tuple(
            pr_body()
            + "\n## Project-specific requirements\n\n"
            + "- [x] **Project-specific: Reviewer approval** — "
            + detail
            + " [status: satisfied]\n"
            for detail in (
                "Independent **review** is optional",
                "Independent<!-- --> review is optional",
                "Independent&#32;review is optional",
                "Independent\u00a0review is optional",
                "Independent\u00adreview is optional",
                "Independent\u200breview is optional",
                "Indepen\u200bdent review is optional",
                "Independent rev\u200biew is optional",
            )
        )

        for body in (
            duplicate_section,
            duplicate_requirement,
            shadowing_detail,
            *rendered_shadowing_details,
        ):
            with self.subTest(body=body[-100:]):
                issues = validate_pull_request(
                    title="feat(process): standardize publication",
                    body=body,
                    branch="feat/standardize-publication",
                    state="ready",
                )
                self.assertTrue(issues)

    def test_rejects_markdown_variants_outside_extension_grammar(self):
        variants = (
            " ## Independent review\n",
            "## Independent review:\n",
            "Independent review\n---\n",
            "<h2>Independent review</h2>\n",
            "* [x] **Independent review** — optional. [status: satisfied]\n",
            "```bad`\n## Independent review\n",
        )

        for extension in variants:
            with self.subTest(extension=extension):
                issues = validate_pull_request(
                    title="feat(process): standardize publication",
                    body=pr_body() + extension,
                    branch="feat/standardize-publication",
                    state="ready",
                )
                self.assertTrue(issues)

    def test_rejects_markerless_hidden_and_weakened_managed_content(self):
        markerless = pr_body().replace(PR_DESCRIPTION_START, "").replace(
            PR_DESCRIPTION_END, ""
        )
        hidden = "Visible preface\n\n<!--\n" + pr_body() + "\n-->\n"
        weakened = pr_body().replace(
            "accepted scope is implemented without unapproved expansion",
            "some scope was considered",
        )

        for body in (markerless, hidden, weakened):
            with self.subTest(body=body[:40]):
                self.assertTrue(
                    validate_pull_request(
                        title="feat(process): standardize publication",
                        body=body,
                        branch="feat/standardize-publication",
                        state="ready",
                    )
                )

    def test_ready_automation_pr_cannot_leave_standard_requirements_pending(self):
        issues = validate_pull_request(
            title="chore: update dependencies",
            body=pr_body("pending"),
            branch="automation/renovate/runtime-packages",
            state="ready",
        )

        self.assertTrue(any("not ready for publication" in issue for issue in issues))

    def test_rejects_a_managed_block_hidden_in_a_code_fence(self):
        canonical = pr_body()
        invalid_indented_close = (
            PR_DESCRIPTION_START
            + "\n```markdown\n    ```\n"
            + canonical.split("\n", 1)[1]
        )

        for body in ("```markdown\n" + canonical + "```\n", invalid_indented_close):
            with self.subTest(body=body[:80]):
                issues = validate_pull_request(
                    title="feat(process): standardize publication",
                    body=body,
                    branch="feat/standardize-publication",
                    state="ready",
                )
                self.assertTrue(issues)

    def test_rejects_managed_content_hidden_in_raw_html_constructs(self):
        canonical = pr_body()
        wrappers = (
            ("<?processing instruction\n", "?>\n"),
            ("<![CDATA[\n", "]]>\n"),
            ("<center>\n", "</center>\n"),
            ("<pre\n", ""),
            ("<script\n", ""),
            ("<center\n", ""),
            ("<source\n", ""),
        )

        for opening, closing in wrappers:
            body = canonical.replace(
                PR_DESCRIPTION_START,
                PR_DESCRIPTION_START + "\n" + opening,
            ).replace(PR_DESCRIPTION_END, closing + PR_DESCRIPTION_END)
            with self.subTest(opening=opening.strip()):
                self.assertTrue(
                    validate_pull_request(
                        title="feat(process): standardize publication",
                        body=body,
                        branch="feat/standardize-publication",
                        state="ready",
                    )
                )

    def test_rejects_prefix_content_and_raw_html_around_standard_sections(self):
        prefixed = "Project preface\n\n" + pr_body()
        raw_html = pr_body().replace("## Summary", "<pre>\n## Summary").replace(
            PR_DESCRIPTION_END, "</pre>\n" + PR_DESCRIPTION_END
        )

        for body in (prefixed, raw_html):
            with self.subTest(body=body[:40]):
                self.assertTrue(
                    validate_pull_request(
                        title="feat(process): standardize publication",
                        body=body,
                        branch="feat/standardize-publication",
                        state="ready",
                    )
                )

    def test_commit_range_reports_the_exact_invalid_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "process-test@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Process Test"], cwd=root, check=True
            )
            tracked = root / "tracked.txt"
            tracked.write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "chore: initialize fixture"],
                cwd=root,
                check=True,
            )
            tracked.write_text("two\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-qam", "Invalid subject"], cwd=root, check=True)

            issues, records = validate_commit_range(
                root,
                branch="fix/publication-gate",
                range_spec="HEAD~1..HEAD",
            )

            self.assertEqual(1, len(records))
            self.assertTrue(any(records[0][0][:12] in issue for issue in issues))

    def test_repository_template_is_the_packaged_standard(self):
        root = Path(__file__).resolve().parent.parent
        self.assertEqual(
            (root / "templates" / "PULL_REQUEST_TEMPLATE.md").read_text(
                encoding="utf-8"
            ),
            (root / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(
                encoding="utf-8"
            ),
        )


if __name__ == "__main__":
    unittest.main()
