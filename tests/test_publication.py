import subprocess
import tempfile
import unittest
from pathlib import Path

from engineering_process.publication import (
    validate_branch,
    validate_commit_range,
    validate_commit_subject,
    validate_pr_title,
    validate_pull_request,
)


def pr_body(status: str = "satisfied") -> str:
    checked = "x" if status != "pending" else " "
    return f"""## Summary

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

- [{checked}] **Scope and contract** — exact accepted scope. [status: {status}]
- [{checked}] **Verification evidence** — current profiles passed. [status: {status}]
- [{checked}] **Independent review** — no open findings. [status: {status}]
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
