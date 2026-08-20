import subprocess
import tempfile
import unittest
from pathlib import Path

from engineering_process.publication import (
    MAX_EVIDENCE_URL_CHARACTERS,
    MAX_MANAGED_LINKS,
    MAX_MANAGED_URL_BYTES,
    PR_DESCRIPTION_END,
    PR_DESCRIPTION_START,
    managed_pull_request_visibility_issues,
    validate_branch,
    validate_commit_range,
    validate_commit_subject,
    validate_pr_title,
    validate_pull_request,
)


def pr_body(status: str = "satisfied") -> str:
    checked = "x" if status != "pending" else " "
    contract_reference = (
        "\n[Evidence: contract](https://evidence.example/contract)\n"
        if status == "satisfied"
        else ""
    )
    verification_reference = (
        "\n[Evidence: verification](https://evidence.example/verification)\n"
        if status == "satisfied"
        else ""
    )
    review_reference = (
        "\n[Evidence: independent review](https://evidence.example/review)\n"
        if status == "satisfied"
        else ""
    )
    return f"""{PR_DESCRIPTION_START}
## Summary

Adopt the shared publication contract.

## Contract and scope

change-contract: publication-standard
{contract_reference}

## Impact and risk

No runtime behavior changes; publication metadata becomes portable.

## Verification

`python -m unittest`
{verification_reference}

## Independent review

Separate reviewer approved checkpoint abc123.
{review_reference}

## Requirements and rules followed

- [{checked}] **Scope and contract** — accepted scope is implemented without unapproved expansion. [status: {status}]
- [{checked}] **Verification evidence** — required current profiles pass on the published checkpoint. [status: {status}]
- [{checked}] **Independent review** — a separate reviewer approved the published checkpoint with no open required finding. [status: {status}]
{PR_DESCRIPTION_END}
"""


class PublicationTests(unittest.TestCase):
    def publication_issues(self, body: str, *, state: str = "ready") -> list[str]:
        return validate_pull_request(
            title="feat(process): standardize publication",
            body=body,
            branch="feat/standardize-publication",
            state=state,
        )

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

    def test_satisfied_claims_require_referenceable_evidence(self):
        body = pr_body()
        for reference in (
            "[Evidence: contract](https://evidence.example/contract)",
            "[Evidence: verification](https://evidence.example/verification)",
            "[Evidence: independent review](https://evidence.example/review)",
        ):
            body = body.replace(reference, "")
        body = body.replace(
            "Separate reviewer approved checkpoint abc123.",
            "Kepler used context portability-review-pr-6-cycle-2. "
            "Review digest: sha256:" + "a" * 64,
        )

        issues = self.publication_issues(body)

        self.assertEqual(
            3,
            sum("requires one [evidence:" in issue for issue in issues),
        )

    def test_evidence_references_are_visible_unique_safe_and_section_owned(self):
        cases = {
            "relative": (
                pr_body().replace(
                    "https://evidence.example/review",
                    ".process/runs/change/review.json",
                ),
                "must use HTTPS",
            ),
            "credentials": (
                pr_body().replace(
                    "https://evidence.example/review",
                    "https://user:secret@evidence.example/review",
                ),
                "no credentials",
            ),
            "duplicate": (
                pr_body().replace(
                    "[Evidence: verification](https://evidence.example/verification)",
                    "[Evidence: verification](https://evidence.example/verification)\n"
                    "[Evidence: verification](https://evidence.example/second)",
                ),
                "must appear exactly once",
            ),
            "unsupported-label": (
                pr_body().replace(
                    "Evidence: verification", "Evidence: checks"
                ),
                "Unsupported evidence reference label",
            ),
            "hidden": (
                pr_body().replace(
                    "[Evidence: verification](https://evidence.example/verification)",
                    "```text\n"
                    "[Evidence: verification](https://evidence.example/verification)\n"
                    "```",
                ),
                "requires one [evidence: verification]",
            ),
            "misplaced": (
                pr_body()
                .replace(
                    "[Evidence: verification](https://evidence.example/verification)",
                    "",
                )
                .replace(
                    "No runtime behavior changes; publication metadata becomes portable.",
                    "No runtime behavior changes; publication metadata becomes portable.\n\n"
                    "[Evidence: verification](https://evidence.example/verification)",
                ),
                "must be in ## Verification",
            ),
        }
        for name, (body, expected) in cases.items():
            with self.subTest(name=name):
                self.assertTrue(
                    any(expected in issue for issue in self.publication_issues(body))
                )

    def test_pending_claim_does_not_publish_completed_reference(self):
        body = pr_body("pending").replace(
            "Separate reviewer approved checkpoint abc123.",
            "Separate reviewer is pending.\n\n"
            "[Evidence: independent review](https://evidence.example/review)",
        )

        issues = self.publication_issues(body, state="draft")

        self.assertTrue(
            any("status pending must not publish" in issue for issue in issues)
        )

    def test_evidence_reference_resources_are_bounded(self):
        oversized_url = (
            "https://evidence.example/" + "x" * MAX_EVIDENCE_URL_CHARACTERS
        )
        issues = self.publication_issues(
            pr_body().replace(
                "https://evidence.example/verification", oversized_url
            )
        )
        self.assertTrue(any("invalid bounded URL" in issue for issue in issues))

        excessive_links = "\n".join(
            f"[Reference {index}](https://evidence.example/{index})"
            for index in range(MAX_MANAGED_LINKS + 1)
        )
        issues = self.publication_issues(
            pr_body().replace(
                "Adopt the shared publication contract.",
                "Adopt the shared publication contract.\n\n" + excessive_links,
            )
        )
        self.assertTrue(any("visible links" in issue for issue in issues))

        link_count = 20
        path_length = MAX_MANAGED_URL_BYTES // link_count
        aggregate_links = "\n".join(
            f"[Aggregate {index}](https://evidence.example/{'x' * path_length}{index})"
            for index in range(link_count)
        )
        issues = self.publication_issues(
            pr_body().replace(
                "Adopt the shared publication contract.",
                "Adopt the shared publication contract.\n\n" + aggregate_links,
            )
        )
        self.assertTrue(any("URL" in issue and "bytes" in issue for issue in issues))

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

    def test_candidate_template_exposes_canonical_evidence_slots(self):
        root = Path(__file__).resolve().parent.parent
        template = (root / "templates" / "PULL_REQUEST_TEMPLATE.md").read_text(
            encoding="utf-8"
        )

        for label in (
            "Evidence: contract",
            "Evidence: verification",
            "Evidence: independent review",
        ):
            self.assertEqual(1, template.count(f"[{label}](https://...)"))
        self.assertEqual([], managed_pull_request_visibility_issues(template))


if __name__ == "__main__":
    unittest.main()
