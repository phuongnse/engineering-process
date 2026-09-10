from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from engineering_process.artifact_standards import resolve_standard
from verification.verify_publication import verify_publication

from engineering_process.publication_compat import (
    branch_issues,
    commit_issues,
    current_branch,
    current_commit_subject,
    validate_pull_request,
    validate_current_source,
    validate_range,
)


CANONICAL_BODY = """## Summary

- Outcome: Preserve the requested behavior.
- Scope: Runtime and regression tests.

## Contract and risk

- Source: https://github.com/example/project/issues/123
- Risk: medium
- Compatibility: Existing callers are unchanged.
- Stack: none

## Verification

- Profiles: `development`, `review`
- Snapshot: `sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`
- Completion receipt: `sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb`

## Independent review

- Verdict: approved
- Cycles: 1
- Blocking findings: 0 open
- Non-blocking dispositions: none

## Completion gate

- [x] Accepted scope is implemented without silent expansion.
- [x] Required profiles pass on the reviewed snapshot.
- [x] Independent review approved with no blocking finding.
- [x] Every non-blocking finding has a recorded disposition.
"""


class PublicationCompatibilityTests(unittest.TestCase):
    def test_branch_and_commit_conventions(self) -> None:
        self.assertEqual([], branch_issues("feature/small-change"))
        self.assertEqual([], branch_issues("automation/renovate/engineering-process"))
        self.assertTrue(branch_issues("main"))
        self.assertEqual([], commit_issues("fix(core): preserve behavior"))
        self.assertTrue(commit_issues("unstructured subject"))

    def validate_body(self, body_text: str, *, state: str = "ready") -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            body = Path(directory) / "body.md"
            body.write_bytes(body_text.encode("utf-8"))
            result = validate_pull_request(
                title="fix(core): preserve behavior",
                branch="fix/preserve-behavior",
                state=state,
                body_path=body,
            )
        return result["issues"]

    def test_pull_request_accepts_canonical_public_evidence(self) -> None:
        self.assertEqual([], self.validate_body(CANONICAL_BODY))

    def test_pull_request_requires_unique_ordered_sections(self) -> None:
        without_contract = CANONICAL_BODY.replace(
            "## Contract and risk\n", "Contract and risk\n", 1
        )
        self.assertIn(
            "pull request body is missing ## Contract and risk",
            self.validate_body(without_contract),
        )

        without_completion = CANONICAL_BODY.replace(
            "## Completion gate\n", "Completion gate\n", 1
        )
        self.assertIn(
            "pull request body is missing ## Completion gate",
            self.validate_body(without_completion),
        )

        repeated = CANONICAL_BODY.replace(
            "## Summary\n", "## Summary\n## Summary\n", 1
        )
        self.assertIn(
            "pull request body repeats ## Summary", self.validate_body(repeated)
        )

        verification = CANONICAL_BODY.index("## Verification")
        review = CANONICAL_BODY.index("## Independent review")
        out_of_order = (
            CANONICAL_BODY[:verification]
            + CANONICAL_BODY[review:]
            + CANONICAL_BODY[verification:review]
        )
        self.assertIn(
            "pull request body sections are out of order",
            self.validate_body(out_of_order),
        )

        unexpected = CANONICAL_BODY.replace(
            "## Verification", "## Screenshots\n\nNone.\n\n## Verification", 1
        )
        self.assertIn(
            "pull request body has unexpected section ## Screenshots",
            self.validate_body(unexpected),
        )

    def test_pull_request_requires_unique_ordered_fields(self) -> None:
        missing = CANONICAL_BODY.replace("- Compatibility:", "- Migration:", 1)
        self.assertIn(
            "pull request body is missing Compatibility in ## Contract and risk",
            self.validate_body(missing),
        )

        repeated = CANONICAL_BODY.replace(
            "- Scope: Runtime and regression tests.",
            "- Scope: Runtime and regression tests.\n- Scope: Documentation.",
            1,
        )
        self.assertIn(
            "pull request body repeats Scope",
            self.validate_body(repeated),
        )

        misplaced_duplicate = CANONICAL_BODY.replace(
            "- Outcome: Preserve the requested behavior.",
            "- Outcome: Preserve the requested behavior.\n- Verdict: approved",
            1,
        )
        self.assertIn(
            "pull request body repeats Verdict",
            self.validate_body(misplaced_duplicate),
        )

        out_of_order = CANONICAL_BODY.replace(
            "- Outcome: Preserve the requested behavior.\n- Scope: Runtime and regression tests.",
            "- Scope: Runtime and regression tests.\n- Outcome: Preserve the requested behavior.",
            1,
        )
        self.assertIn(
            "pull request body fields are out of order in ## Summary",
            self.validate_body(out_of_order),
        )

    def test_pull_request_rejects_noncanonical_metadata_lines(self) -> None:
        for metadata in (
            "- Reviewer: codex-reviewer-114",
            "- Actor ID: codex-root",
            "- Context ID: public-review-metadata-cycle-1",
            "- Reviewer actor ID: codex-root",
            "- **Actor ID**: codex-root",
            '"actorId": "codex-root"',
            "| Actor ID | codex-root |",
            "- Reviewer handle: codex-reviewer-114",
            "- Report path: .process/runs/change/review-1.json",
            "Evidence lives at `.process/runs/change/review-1.json`.",
            "Reviewed by `codex-reviewer-114`.",
        ):
            with self.subTest(metadata=metadata):
                body = CANONICAL_BODY.replace(
                    "- Verdict: approved", f"{metadata}\n- Verdict: approved", 1
                )
                self.assertTrue(
                    any(
                        issue.startswith(
                            "pull request body has unsupported visible content at line "
                        )
                        for issue in self.validate_body(body)
                    )
                )

    def test_ready_pull_request_requires_completed_checklist(self) -> None:
        unchecked = CANONICAL_BODY.replace(
            "- [x] Required profiles", "- [ ] Required profiles", 1
        )
        self.assertIn(
            "ready pull request has unchecked item: Required profiles pass on the reviewed snapshot.",
            self.validate_body(unchecked),
        )
        self.assertEqual([], self.validate_body(unchecked, state="draft"))

    def test_pull_request_requires_ordered_checklist_in_completion_section(self) -> None:
        first = "- [x] Accepted scope is implemented without silent expansion."
        second = "- [x] Required profiles pass on the reviewed snapshot."
        out_of_order = CANONICAL_BODY.replace(
            f"{first}\n{second}", f"{second}\n{first}", 1
        )
        self.assertIn(
            "pull request body checklist items are out of order",
            self.validate_body(out_of_order),
        )

        misplaced = CANONICAL_BODY.replace(f"\n{first}", "", 1).replace(
            "- Outcome: Preserve the requested behavior.",
            f"{first}\n- Outcome: Preserve the requested behavior.",
            1,
        )
        self.assertIn(
            "pull request body misplaces checklist item: "
            "Accepted scope is implemented without silent expansion.",
            self.validate_body(misplaced),
        )

        mixed_hierarchy = CANONICAL_BODY.replace(f"\n{first}", "", 1).replace(
            "## Completion gate", f"{first}\n\n## Completion gate", 1
        )
        self.assertIn(
            "pull request body misplaces checklist item: "
            "Accepted scope is implemented without silent expansion.",
            self.validate_body(mixed_hierarchy),
        )

    def test_pull_request_uses_only_rendered_top_level_structure(self) -> None:
        fenced = f"```markdown\n{CANONICAL_BODY}```\n"
        self.assertIn(
            "pull request body contains a Markdown fence",
            self.validate_body(fenced),
        )
        self.assertIn(
            "pull request body is missing ## Summary", self.validate_body(fenced)
        )

        hidden = f"<!--\n{CANONICAL_BODY}"
        hidden_issues = self.validate_body(hidden)
        self.assertIn("pull request body has an unclosed HTML comment", hidden_issues)
        self.assertIn("pull request body is missing ## Summary", hidden_issues)

        closed_hidden = f"<!--\n{CANONICAL_BODY}-->\n"
        self.assertIn(
            "pull request body is missing ## Summary",
            self.validate_body(closed_hidden),
        )

        unclosed_fence = CANONICAL_BODY.replace(
            "## Summary", "```markdown\n## Summary", 1
        )
        self.assertIn(
            "pull request body has an unclosed Markdown fence",
            self.validate_body(unclosed_fence),
        )

        for separator in ("\v", "\f", "\x85", "\u2028", "\u2029"):
            with self.subTest(separator=ascii(separator)):
                separated = CANONICAL_BODY.replace("\n", separator)
                separated_issues = self.validate_body(separated)
                self.assertIn(
                    "pull request body contains a non-Markdown line separator",
                    separated_issues,
                )
                self.assertIn(
                    "pull request body is missing ## Summary", separated_issues
                )

    def test_pull_request_accepts_commonmark_line_endings(self) -> None:
        self.assertEqual([], self.validate_body(CANONICAL_BODY.replace("\n", "\r\n")))
        self.assertEqual([], self.validate_body(CANONICAL_BODY.replace("\n", "\r")))

    def test_pull_request_allows_reviewer_words_in_technical_values(self) -> None:
        technical = CANONICAL_BODY.replace(
            "Runtime and regression tests.",
            "Update the access-reviewer-service package.",
            1,
        )
        self.assertEqual([], self.validate_body(technical))

    def test_optional_issue_reference_follows_the_checklist(self) -> None:
        referenced = f"{CANONICAL_BODY}\nRefs #123.\n"
        self.assertEqual([], self.validate_body(referenced))

        misplaced = CANONICAL_BODY.replace(
            "## Summary", "Refs #123.\n\n## Summary", 1
        )
        self.assertIn(
            "pull request body issue reference must follow the checklist",
            self.validate_body(misplaced),
        )

        repeated = f"{CANONICAL_BODY}\nRefs #123.\nRefs #124.\n"
        self.assertIn(
            "pull request body repeats its issue reference",
            self.validate_body(repeated),
        )

    def test_final_adoption_can_close_multiple_issues_only_when_ready(self) -> None:
        closing = (
            f"{CANONICAL_BODY}\n"
            "Closes example/process#123, closes #456, closes other/repository#789.\n"
        )
        self.assertEqual([], self.validate_body(closing))
        self.assertIn(
            "draft pull request cannot close issues",
            self.validate_body(closing, state="draft"),
        )

        unchecked = closing.replace(
            "- [x] Required profiles", "- [ ] Required profiles", 1
        )
        self.assertIn(
            "ready pull request has unchecked item: Required profiles pass on the reviewed snapshot.",
            self.validate_body(unchecked),
        )

        for malformed in (
            "Closes example/process#123, #456.",
            "Closes example/process#123, Closes #456.",
            "Fixes example/process#123.",
            "Closes example/process#0.",
        ):
            with self.subTest(malformed=malformed):
                issues = self.validate_body(f"{CANONICAL_BODY}\n{malformed}\n")
                self.assertTrue(
                    any(
                        issue.startswith(
                            "pull request body has unsupported visible content at line "
                        )
                        for issue in issues
                    )
                )

    def test_pull_request_ignores_template_comments(self) -> None:
        body = CANONICAL_BODY.replace(
            "- Outcome: Preserve the requested behavior.",
            "<!-- Do not publish .process/runs or Reviewer: values. -->\n"
            "- Outcome: Preserve the requested behavior.",
            1,
        )
        self.assertEqual([], self.validate_body(body))

    def test_range_checks_every_commit_subject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "tests@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Tests"], cwd=root, check=True
            )
            (root / "file.txt").write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "chore: initial"], cwd=root, check=True
            )
            base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            (root / "file.txt").write_text("two\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-qam", "fix(core): update"], cwd=root, check=True)
            result = validate_range(root, "fix/update", f"{base}..HEAD")
        self.assertEqual([], result["issues"])
        self.assertEqual(1, len(result["commits"]))

    def test_current_source_checks_the_branch_head_and_comparison_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True, timeout=30)
            subprocess.run(
                ["git", "config", "user.email", "tests@example.invalid"],
                cwd=root,
                check=True, capture_output=True, timeout=30,
            )
            subprocess.run(
                ["git", "config", "user.name", "Tests"], cwd=root, check=True
            )
            (root / "file.txt").write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, timeout=30)
            subprocess.run(
                ["git", "commit", "-qm", "chore: initial"], cwd=root, check=True
            )
            base = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=30
            ).strip()
            (root / "file.txt").write_text("two\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-qam", "fix(core): update"], cwd=root, check=True, capture_output=True, timeout=30)
            subprocess.run(
                ["git", "branch", "-M", "fix/source_gate"], cwd=root, check=True
            )
            observed_branch = current_branch(root)
            observed_subject = current_commit_subject(root)
            result = validate_current_source(root, base)
            invalid_range = validate_current_source(root, "0" * 40)
            subprocess.run(
                ["git", "commit", "--amend", "-qm", " fix: leading-space"],
                cwd=root,
                check=True, capture_output=True, timeout=30,
            )
            invalid = validate_current_source(root, base)
            invalid_subject = current_commit_subject(root)

        self.assertEqual([], result["issues"])
        self.assertEqual("fix/source_gate", result["branch"])
        self.assertEqual("fix(core): update", result["subject"])
        self.assertTrue(result["range"].startswith(f"{base}.."))
        self.assertRegex(result["range"].split("..", 1)[1], r"^[0-9a-f]{40}$")
        self.assertEqual("fix/source_gate", observed_branch)
        self.assertEqual("fix(core): update", observed_subject)
        self.assertTrue(
            any(
                issue.startswith("git cannot resolve commit range:")
                for issue in invalid_range["issues"]
            )
        )
        self.assertEqual(" fix: leading-space", invalid_subject)
        self.assertIn(
            "commit subject must use Conventional Commit style", invalid["issues"]
        )

    def test_pr_ci_uses_selected_body_and_exact_head_instead_of_checkout_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args: str) -> str:
                return subprocess.run(["git", *args], cwd=root, check=True,
                                      capture_output=True, text=True, timeout=30).stdout.strip()

            git("init", "-q")
            git("config", "user.email", "tests@example.invalid")
            git("config", "user.name", "Tests")
            standard = resolve_standard(None, Path(__file__).resolve().parent.parent, "pull-request").document
            standard["id"] = "consumer.pr"
            standard["rules"]["sections"][0]["fields"][0]["label"] = "Result"
            (root / ".process").mkdir()
            (root / ".process/pr.json").write_text(json.dumps(standard), encoding="utf-8")
            (root / ".process/standards.json").write_text(json.dumps({
                "schemaVersion": 1, "artifacts": {"pull-request": {"path": ".process/pr.json"}},
            }), encoding="utf-8")
            git("add", ".")
            git("commit", "-qm", "chore: initial")
            base = git("rev-parse", "HEAD")
            git("commit", "--allow-empty", "-qm", "fix: reviewed change")
            head = git("rev-parse", "HEAD")
            git("commit", "--allow-empty", "-qm", "unstructured checkout commit")
            checkout = git("rev-parse", "HEAD")
            context = {"PUBLICATION_BRANCH": "fix/metadata", "PUBLICATION_TITLE": "fix: reviewed change",
                       "PUBLICATION_BODY": CANONICAL_BODY.replace("- Outcome:", "- Result:"),
                       "PUBLICATION_DRAFT": "false", "PUBLICATION_BASE": base, "PUBLICATION_HEAD": head}
            with patch.dict(os.environ, context):
                self.assertEqual([], verify_publication(root, pull_request=True))
            with patch.dict(os.environ, {**context, "PUBLICATION_BODY": CANONICAL_BODY}):
                self.assertTrue(verify_publication(root, pull_request=True))
            with patch.dict(os.environ, {**context, "PUBLICATION_BASE": checkout, "PUBLICATION_HEAD": checkout}):
                self.assertIn("commit subject must use Conventional Commit style",
                              verify_publication(root, pull_request=True))


if __name__ == "__main__":
    unittest.main()
