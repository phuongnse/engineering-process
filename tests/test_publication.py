import subprocess
import tempfile
import unittest
from pathlib import Path

from engineering_process.publication import (
    PR_DESCRIPTION_END,
    PR_DESCRIPTION_START,
    validate_branch,
    validate_commit_range,
    validate_commit_subject,
    validate_pr_title,
    validate_pull_request,
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
            "- [x] **UI evidence** — no UI surface changed. "
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

    def test_project_extensions_cannot_redefine_common_policy(self):
        duplicate_section = pr_body() + (
            "\n## Independent review\n\nIndependent review is not required here.\n"
        )
        duplicate_requirement = pr_body() + (
            "\n- [x] **Independent review** — project-local review is optional. "
            "[status: satisfied]\n"
        )

        for body in (duplicate_section, duplicate_requirement):
            with self.subTest(body=body[-100:]):
                issues = validate_pull_request(
                    title="feat(process): standardize publication",
                    body=body,
                    branch="feat/standardize-publication",
                    state="ready",
                )
                self.assertTrue(any("must not duplicate" in issue for issue in issues))

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
        issues = validate_pull_request(
            title="feat(process): standardize publication",
            body="```markdown\n" + pr_body() + "```\n",
            branch="feat/standardize-publication",
            state="ready",
        )

        self.assertTrue(issues)

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
