from __future__ import annotations

import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from engineering_process.artifact_standards import resolve_standard
from engineering_process.automation_name import render_name
from engineering_process.cli import main
from engineering_process.contracts import ProcessError, digest_json, formatted_json_bytes
from engineering_process.issue import render_issue
from engineering_process.lifecycle import (
    begin_implementation,
    finish_change,
    register_plan,
    start_change,
    start_review,
    submit_review,
    verify_change,
)
from engineering_process.pr_description import (
    body_issues,
    build_pr_description_data,
    render_description,
    render_renovate_preset,
    render_template,
)
from engineering_process.production_engineering import load_invariant_floor
from engineering_process.publication_compat import validate_pull_request
from engineering_process.release_notes import render_notes
from engineering_process.repository import repository_snapshot


ROOT = Path(__file__).resolve().parent.parent


def issue_data(state: str) -> dict:
    data = {
        "schemaVersion": 1,
        "title": "Provide bounded issue evidence",
        "fields": {
            "context": "Consumer issues are lifecycle sources.",
            "expected-outcome": "Use one selected issue standard.",
            "evidence": "The current catalog has no issue artifact.",
            "scope": "Issue records only; no tracker API.",
            "acceptance-criteria": "Open and closed records validate.",
            "references": "https://example.com/issues/12",
        },
        "checks": {},
    }
    if state == "closed":
        data["fields"].update({
            "resolution": "The selected standard now owns issue records.",
            "implementing-change": "https://example.com/pull/13",
            "verification": "Development and review profiles passed.",
            "release": "none",
            "adoption": "none",
            "consumer-confirmation": "none",
            "remaining-risks": "None.",
            "follow-ups": "https://example.com/issues/14, https://example.com/issues/15",
        })
    return data


class ArtifactStandardsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True, timeout=30)

    def write(self, relative: str, value: object) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(formatted_json_bytes(value))
        return path

    def select(self, document: dict, *, path: str = ".process/company-standard.json"):
        self.write(path, document)
        self.write(".process/standards.json", {"schemaVersion": 1, "artifacts": {document["artifact"]: {"path": path}}})
        return resolve_standard(self.root, ROOT, document["artifact"])

    def cli(self, *arguments: str) -> tuple[int, dict]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["artifact", *arguments, "--project-root", str(self.root), "--process-root", str(ROOT), "--json"])
        return code, json.loads(output.getvalue())

    def test_ready_rejects_generated_placeholders_even_with_all_checks_completed(self) -> None:
        standard = resolve_standard(self.root, ROOT, "pull-request")
        body = render_description(standard).replace("- [ ]", "- [x]")
        self.assertEqual([], body_issues(body, "draft", standard))
        for pending in ("pending", "PENDING", "pending...", "pending…"):
            with self.subTest(pending=pending):
                path = self.root / "pr.md"
                path.write_text(body.replace("pending", pending), encoding="utf-8")
                result = validate_pull_request(title="fix: complete evidence", branch="fix/evidence", state="ready", body_path=path, project_root=self.root, process_root=ROOT)
                self.assertTrue(any("unresolved value" in issue for issue in result["issues"]))
        # Ordinary technical prose containing that word is not an unresolved token.
        self.assertEqual([], body_issues(body.replace("pending", "Handle pending requests correctly."), "ready", standard))

    def test_override_changes_generation_validation_and_renovate_together(self) -> None:
        document = deepcopy(resolve_standard(None, ROOT, "pull-request").document)
        document.update(id="company.pr", version=2)
        summary = document["rules"]["sections"][0]
        summary["heading"] = "## Changes delivered"
        summary["fields"].reverse()
        summary["fields"][0]["label"] = "Changed surfaces"
        summary["fields"].append({"id": "rollout", "label": "Rollout", "description": "Consumer rollout decision."})
        standard = self.select(document)
        template = render_template(standard)
        self.assertIn("## Changes delivered", template)
        self.assertLess(template.index("- Changed surfaces:"), template.index("- Outcome:"))
        preset = render_renovate_preset(standard)
        self.assertIn("- Changed surfaces: {{#each upgrades}}", preset["prHeader"])
        self.assertIn("- Rollout: pending", preset["prHeader"])
        data = {"schemaVersion": 1, "fields": {field["id"]: "Reviewed consumer evidence." for section in standard.rules["sections"] for field in section["fields"]}, "checks": {check["id"]: True for section in standard.rules["sections"] for check in section["checks"]}}
        body = render_description(standard, data, state="ready")
        path = self.root / "pr.md"
        path.write_bytes(body.encode("utf-8"))
        result = validate_pull_request(title="feat: use consumer standard", branch="feat/standard", state="ready", body_path=path, project_root=self.root, process_root=ROOT)
        self.assertEqual([], result["issues"])
        self.assertEqual("company.pr", result["standard"]["id"])
        self.assertTrue(body_issues(body, "ready", resolve_standard(None, ROOT, "pull-request")))
        data["fields"].pop("rollout")
        with self.assertRaisesRegex(ProcessError, "unresolved value for Rollout"):
            render_description(standard, data, state="ready")

    def test_explicit_invalid_selections_never_fall_back(self) -> None:
        for reference in ({"builtin": "pull-request@999"}, {"path": ".process/missing.json"}, {"path": "../outside.json"}, {"builtin": "release-notes@1"}):
            with self.subTest(reference=reference):
                self.write(".process/standards.json", {"schemaVersion": 1, "artifacts": {"pull-request": reference}})
                with self.assertRaises(ProcessError):
                    resolve_standard(self.root, ROOT, "pull-request")

    def test_schema_and_relationship_errors_are_rejected(self) -> None:
        original = resolve_standard(None, ROOT, "pull-request").document
        changes = []
        unknown = deepcopy(original); unknown["adapter"] = "arbitrary-code"; changes.append(unknown)
        duplicate = deepcopy(original); duplicate["rules"]["sections"].append(deepcopy(duplicate["rules"]["sections"][0])); changes.append(duplicate)
        malformed = deepcopy(original); malformed["rules"]["sections"][0]["heading"] += "\n"; changes.append(malformed)
        unsupported = deepcopy(original); unsupported["schemaVersion"] = 2; changes.append(unsupported)
        for document in changes:
            with self.subTest(document=document):
                with self.assertRaises(ProcessError):
                    self.select(document)

    def test_ignored_or_lifecycle_state_overrides_cannot_escape_snapshot_binding(self) -> None:
        document = resolve_standard(None, ROOT, "pull-request").document
        (self.root / ".gitignore").write_text("hidden/\n", encoding="utf-8")
        for path in ("hidden/standard.json", ".process/runs/standard.json", ".process/receipts/standard.json"):
            with self.subTest(path=path), self.assertRaisesRegex(ProcessError, "snapshot"):
                self.select(document, path=path)

    def test_symlink_override_is_rejected(self) -> None:
        document = resolve_standard(None, ROOT, "pull-request").document
        actual = self.write("actual.json", document)
        link = self.root / "linked.json"
        try:
            link.symlink_to(actual)
        except OSError:
            self.skipTest("host cannot create symlinks")
        self.write(".process/standards.json", {"schemaVersion": 1, "artifacts": {"pull-request": {"path": "linked.json"}}})
        with self.assertRaisesRegex(ProcessError, "links"):
            resolve_standard(self.root, ROOT, "pull-request")

    def test_override_changes_both_standard_digest_and_repository_snapshot(self) -> None:
        document = deepcopy(resolve_standard(None, ROOT, "pull-request").document)
        standard = self.select(document)
        before = repository_snapshot(self.root)
        document["rules"]["sections"][0]["heading"] = "## Revised scope"
        changed = self.select(document)
        self.assertNotEqual(standard.metadata["digest"], changed.metadata["digest"])
        self.assertNotEqual(before["fingerprint"], repository_snapshot(self.root)["fingerprint"])

    def test_another_document_kind_can_reuse_selection_without_lifecycle_changes(self) -> None:
        document = deepcopy(resolve_standard(None, ROOT, "pull-request").document)
        document.update(id="company.decision", artifact="decision-record")
        document["rules"] = {"sections": [{"heading": "## Decision", "fields": [{"id": "decision", "label": "Decision", "description": "Accepted decision."}], "checks": []}], "issueReferences": False}
        self.select(document)
        data = self.write("decision.json", {"schemaVersion": 1, "fields": {"decision": "Use the existing boundary."}, "checks": {}})
        path = self.root / "decision.md"
        code, report = self.cli("render", "--artifact", "decision-record", "--data-file", str(data), "--output", str(path))
        self.assertEqual(0, code, report)
        code, verified = self.cli("validate", "--artifact", "decision-record", "--data-file", str(data), "--body-file", str(path))
        self.assertEqual(0, code, verified)
        self.assertEqual(report["standard"], verified["standard"])
        self.assertEqual(report["artifactDigest"], verified["artifactDigest"])

    def test_release_override_and_source_records_determine_verified_bytes(self) -> None:
        document = deepcopy(resolve_standard(None, ROOT, "release-notes").document)
        document.update(id="company.release", version=3)
        document["rules"]["groups"].reverse()
        document["rules"]["groups"][0]["heading"] = "Corrections"
        document["rules"]["sections"].append({"id": "security", "heading": "Security impact"})
        self.select(document)
        data = {"schemaVersion": 1, "title": "Example v1.1.0", "introduction": "Changes since v1.0.0.", "changes": [{"type": "fix", "summary": "Keep literal *text* and 1. markers.", "source": "issue #10"}, {"type": "capability", "summary": "Add the requested behavior.", "source": "https://example.com/issues/12"}], "sections": {"upgrade": "No migration required.", "security": "No security behavior changed."}}
        data_path = self.write("release-data.json", data)
        path = self.root / "notes.md"
        code, rendered = self.cli("render", "--artifact", "release-notes", "--data-file", str(data_path), "--output", str(path))
        self.assertEqual(0, code, rendered)
        self.assertIn(b"## Corrections", path.read_bytes())
        self.assertNotIn(b"\r", path.read_bytes())
        code, verified = self.cli("validate", "--artifact", "release-notes", "--data-file", str(data_path), "--body-file", str(path))
        self.assertEqual(0, code, verified)
        self.assertEqual(rendered["standard"], verified["standard"])
        data["sections"]["security"] = "Additional review was required."
        self.write("release-data.json", data)
        self.assertEqual(1, self.cli("validate", "--artifact", "release-notes", "--data-file", str(data_path), "--body-file", str(path))[0])
        self.assertEqual(2, self.cli("validate", "--artifact", "release-notes", "--body-file", str(path))[0])
        data["sections"].pop("security")
        self.write("release-data.json", data)
        self.assertEqual(2, self.cli("render", "--artifact", "release-notes", "--data-file", str(data_path), "--output", str(path))[0])

    def test_invalid_data_does_not_overwrite_output(self) -> None:
        data = self.write("invalid.json", None)
        output = self.root / "document.md"
        output.write_bytes(b"keep me\n")
        for kind in ("pull-request", "release-notes", "automation-name", "issue"):
            with self.subTest(kind=kind):
                code, result = self.cli("render", "--artifact", kind, "--data-file", str(data), "--output", str(output))
                self.assertEqual(2, code, result)
                self.assertEqual(b"keep me\n", output.read_bytes())

    def test_issue_default_renders_open_and_closed_records_with_exact_validation(self) -> None:
        open_data = issue_data("open")
        source = self.write("issue.json", open_data)
        body = self.root / "issue.md"
        code, rendered = self.cli("render", "--artifact", "issue", "--state", "open", "--data-file", str(source), "--output", str(body))
        self.assertEqual(0, code, rendered)
        self.assertTrue(body.read_bytes().startswith(b"# Provide bounded issue evidence\n\n## Request\n"))
        code, checked = self.cli("validate", "--artifact", "issue", "--state", "open", "--data-file", str(source), "--body-file", str(body))
        self.assertEqual(0, code, checked)
        self.assertEqual(rendered["standard"], checked["standard"])
        self.assertEqual(rendered["artifactDigest"], checked["artifactDigest"])

        closed = issue_data("closed")
        closed_source = self.write("closed.json", closed)
        self.assertEqual(0, self.cli("render", "--artifact", "issue", "--state", "closed", "--data-file", str(closed_source), "--output", str(body))[0])
        self.assertIn(b"## Closure evidence", body.read_bytes())
        self.assertEqual(0, self.cli("validate", "--artifact", "issue", "--state", "closed", "--data-file", str(closed_source), "--body-file", str(body))[0])

    def test_issue_reference_grammar_is_checked_before_render_and_validation(self) -> None:
        valid = (
            "https://Example.com:443/issues/12?view=full#comment-2",
            "https://[2001:db8::1]:8443/change/12?next=%2Fdocs#result",
            "https://127.0.0.1:65535/record",
            "https://service_name.example:0/record",
            "https://xn--bcher-kva.example/record",
            "https://%65xample.com/record",
        )
        invalid = (
            "https://example.com:invalid/record",
            "https://example.com:65536/record",
            "https://example.com:-1/record",
            "https://example.com:４４３/record",
            "https:///record",
            "https://:443/record",
            "https://[::1]suffix/record",
            "https://[invalid]/record",
            "https://bad<host>/record",
            "https://%ZZ.example/record",
            "https://user:password@example.com/record",
            "https://example.com/a b",
            "\x00https://example.com/record",
            "https://example.com/rec\x01ord",
            "https://example.com/record\x7f",
            "https://example.com/record\x80",
        )
        body = self.root / "issue.md"
        for state, field in (("open", "references"), ("closed", "references"), ("closed", "implementing-change")):
            for value in valid + invalid:
                with self.subTest(state=state, field=field, value=value):
                    data = issue_data(state)
                    data["fields"][field] = value if field == "implementing-change" else "https://example.com/first, " + value
                    source = self.write("issue.json", data)
                    body.write_bytes(b"preserve existing output\n")
                    arguments = ("--artifact", "issue", "--state", state, "--data-file", str(source))
                    rendered_code, rendered = self.cli("render", *arguments, "--output", str(body))
                    checked_code, checked = self.cli("validate", *arguments, "--body-file", str(body))
                    expected = 0 if value in valid else 2
                    self.assertEqual(expected, rendered_code, rendered)
                    self.assertEqual(expected, checked_code, checked)
                    if expected:
                        self.assertEqual(b"preserve existing output\n", body.read_bytes())
                    else:
                        self.assertEqual(rendered["artifactDigest"], checked["artifactDigest"])

    def test_issue_prefix_cannot_replace_title_in_either_state(self) -> None:
        document = deepcopy(resolve_standard(None, ROOT, "issue").document)
        document.update(id="consumer.issue", version=2)
        document["rules"]["title"]["prefix"] = "[work] "
        self.select(document)
        body = self.root / "issue.md"
        for state in ("open", "closed"):
            for title in ("[work] ", "[work]    ", "[work] \t", "[work] pending", "[work] PENDING…", "[work] Concrete outcome"):
                with self.subTest(state=state, title=title):
                    data = issue_data(state)
                    data["title"] = title
                    source = self.write("issue.json", data)
                    body.write_bytes(b"preserve existing output\n")
                    arguments = ("--artifact", "issue", "--state", state, "--data-file", str(source))
                    expected = 0 if title == "[work] Concrete outcome" else 2
                    self.assertEqual(expected, self.cli("render", *arguments, "--output", str(body))[0])
                    self.assertEqual(expected, self.cli("validate", *arguments, "--body-file", str(body))[0])
                    if expected:
                        self.assertEqual(b"preserve existing output\n", body.read_bytes())

    def test_issue_override_title_state_and_reference_rules_share_one_authority(self) -> None:
        document = deepcopy(resolve_standard(None, ROOT, "issue").document)
        document.update(id="consumer.issue", version=2)
        document["rules"]["title"]["prefix"] = "[work] "
        document["rules"]["states"]["open"]["sections"][0]["heading"] = "## Consumer request"
        document["rules"]["states"]["open"]["sections"][0]["fields"].reverse()
        standard = self.select(document)
        data = {
            "schemaVersion": 1,
            "title": "[work] Standardize issue records",
            "fields": {
                "context": "Consumer-owned context.",
                "expected-outcome": "Selected rules control output.",
                "evidence": "A real consumer request.",
                "scope": "One artifact adapter.",
                "acceptance-criteria": "Override renders and validates.",
                "references": "none",
            },
            "checks": {},
        }
        rendered = render_issue(standard, data)
        self.assertIn("## Consumer request", rendered)
        self.assertLess(rendered.index("### Expected outcome"), rendered.index("### Context"))
        invalid = deepcopy(data); invalid["title"] = "Missing prefix"
        with self.assertRaisesRegex(ProcessError, "must start"):
            render_issue(standard, invalid)
        for title in ("[work] pending", "[work] PENDING…"):
            invalid = deepcopy(data); invalid["title"] = title
            with self.subTest(title=title), self.assertRaisesRegex(ProcessError, "unresolved title"):
                render_issue(standard, invalid)
        invalid = deepcopy(data); invalid["fields"]["references"] = "http://example.com/issues/1"
        with self.assertRaisesRegex(ProcessError, "durable HTTPS"):
            render_issue(standard, invalid)
        invalid = deepcopy(data); invalid["fields"]["scope"] = "pending"
        with self.assertRaisesRegex(ProcessError, "unresolved"):
            render_issue(standard, invalid)
        source = self.write("issue-state.json", data)
        self.assertEqual(2, self.cli("render", "--artifact", "issue", "--state", "ready", "--data-file", str(source), "--output", str(self.root / "issue.md"))[0])
        name = self.write("name-state.json", {"schemaVersion": 1, "components": {"owner": "acme", "role": "bot"}})
        self.assertEqual(2, self.cli("render", "--artifact", "automation-name", "--state", "open", "--data-file", str(name), "--output", str(self.root / "name.txt"))[0])

    def test_automation_name_default_override_and_exact_verification(self) -> None:
        data = {"schemaVersion": 1, "components": {"owner": "Acme", "role": "Dependency-Updates"}}
        source = self.write("name.json", data)
        code, result = self.cli("render", "--artifact", "automation-name", "--data-file", str(source))
        self.assertEqual(0, code, result)
        self.assertEqual("acme-dependency-updates\n", result["content"])
        document = deepcopy(resolve_standard(None, ROOT, "automation-name").document)
        document.update(id="consumer.automation-name", version=2)
        document["rules"].update(components=["role", "owner"], separator=".", case="preserve", maxLength=40)
        self.select(document)
        name = self.root / "app-name.txt"
        code, rendered = self.cli("render", "--artifact", "automation-name", "--data-file", str(source), "--output", str(name))
        self.assertEqual(0, code, rendered)
        self.assertEqual(b"Dependency-Updates.Acme\n", name.read_bytes())
        code, checked = self.cli("validate", "--artifact", "automation-name", "--data-file", str(source), "--body-file", str(name))
        self.assertEqual(0, code, checked)
        self.assertEqual(rendered["standard"], checked["standard"])
        name.write_bytes(b"unselected-name\n")
        self.assertEqual(1, self.cli("validate", "--artifact", "automation-name", "--data-file", str(source), "--body-file", str(name))[0])
        document["rules"]["maxLength"] = 5
        with self.assertRaisesRegex(ProcessError, "length limit"):
            render_name(self.select(document), data)

    def test_automation_name_rejects_undeclared_or_invalid_components_and_rules(self) -> None:
        standard = resolve_standard(None, ROOT, "automation-name")
        for components in ({"owner": "acme"}, {"owner": "acme", "role": "bot", "extra": "unused"}, {"owner": "acme\n", "role": "bot"}, {"owner": "acme", "role": ""}, {"owner": "acme", "role": "a" * 65}):
            with self.subTest(components=components), self.assertRaises(ProcessError):
                render_name(standard, {"schemaVersion": 1, "components": components})
        for key, value in (("components", ["owner", "owner"]), ("separator", "/"), ("case", "guess"), ("maxLength", 0)):
            document = deepcopy(standard.document)
            document["rules"][key] = value
            with self.subTest(rule=key), self.assertRaises(ProcessError):
                self.select(document)
        document = deepcopy(standard.document)
        document["pendingValues"] = ["pending"]
        with self.assertRaises(ProcessError):
            self.select(document)

    def test_release_ready_requires_resolved_values_and_known_change_types(self) -> None:
        standard = resolve_standard(None, ROOT, "release-notes")
        data = {"schemaVersion": 1, "title": "Example", "introduction": "Changes in this release.", "changes": [{"type": "fix", "summary": "Correct the behavior.", "source": "issue-1"}], "sections": {"upgrade": "pending"}}
        self.assertIn("pending", render_notes(standard, data, state="draft"))
        with self.assertRaisesRegex(ProcessError, "unresolved"):
            render_notes(standard, data)
        data["sections"]["upgrade"] = "No migration required."
        data["changes"][0]["type"] = "unclassified"
        with self.assertRaisesRegex(ProcessError, "unsupported change types"):
            render_notes(standard, data)

    def test_standard_and_ready_data_capacities_match_for_fields_and_checks(self) -> None:
        for kind in ("fields", "checks"):
            for count in (128, 129, 512):
                with self.subTest(kind=kind, count=count):
                    document = deepcopy(resolve_standard(None, ROOT, "pull-request").document)
                    entries = [{"id": f"item-{index}", "label": f"Item {index}", **({"description": "Required consumer value."} if kind == "fields" else {})} for index in range(count)]
                    document["rules"]["sections"] = [
                        {"heading": f"## Section {start // 32}", "fields": [], "checks": [], kind: entries[start:start + 32]}
                        for start in range(0, count, 32)
                    ]
                    standard = self.select(document)
                    data = {"schemaVersion": 1, "fields": {}, "checks": {}}
                    data[kind] = {entry["id"]: "Recorded consumer result." if kind == "fields" else True for entry in entries}
                    body = render_description(standard, data, state="ready")
                    self.assertEqual([], body_issues(body, "ready", standard))
                    if count == 512:
                        data[kind]["extra"] = "Recorded result." if kind == "fields" else True
                        with self.assertRaisesRegex(ProcessError, "too many properties"):
                            render_description(standard, data, state="ready")
                        document["rules"]["sections"].append({"heading": "## Extra section", "fields": [], "checks": [], kind: [entries[0]]})
                        with self.assertRaises(ProcessError):
                            self.select(document)


    def test_pr_evidence_authoring_reuses_canonical_facts_and_enforces_boundaries(self) -> None:
        subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Tests"], cwd=self.root, check=True)

        project = {
            "schemaVersion": 5,
            "project": "sample",
            "lifecycle": {"requiredProfiles": ["development", "review"]},
            "profiles": {
                "development": [
                    {
                        "id": "unit",
                        "run": [sys.executable, "-c", "raise SystemExit(0)"],
                        "timeoutSeconds": 10,
                    }
                ],
                "review": [
                    {
                        "id": "contract",
                        "run": [sys.executable, "-c", "raise SystemExit(0)"],
                        "timeoutSeconds": 10,
                    }
                ],
            },
        }
        self.write(".process/project.json", project)
        (self.root / "product.txt").write_text("accepted\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=self.root, check=True)

        contract = {
            "schemaVersion": 5,
            "id": "sample-change",
            "summary": "Make one sample change",
            "source": "https://example.com/issues/100",
            "comparisonBase": "HEAD",
            "risk": "low",
            "affectedProjects": ["sample"],
            "acceptanceCriteria": [
                {"id": "works", "outcome": "The accepted behavior works"}
            ],
            "requiredProfiles": ["development", "review"],
        }
        contract_path = self.write("change.json", contract)

        invariant_ids = [
            item["id"] for item in load_invariant_floor(ROOT)["invariants"]
        ]
        plan = {
            "schemaVersion": 5,
            "changeId": "sample-change",
            "contractDigest": digest_json(contract),
            "approach": "Make and verify the bounded change.",
            "workItems": [
                {
                    "id": "implementation",
                    "outcome": "Implement accepted behavior",
                    "affectedPaths": ["product.txt"],
                }
            ],
            "risks": [],
            "productionEngineering": [
                {
                    "id": invariant_id,
                    "applicability": "applicable",
                    "rationale": "The sample exercises this production boundary.",
                    "evidenceWorkItems": ["implementation"],
                }
                for invariant_id in invariant_ids
            ],
        }
        plan_path = self.write("plan.json", plan)

        # Round 1: start, plan, implement, verify, review with changes-requested
        start_change(self.root, ROOT, project, contract_path, actor_id="author", context_id="author-context", kind="agent")
        register_plan(self.root, ROOT, "sample-change", plan_path, actor_id="author", context_id="author-context", kind="agent")
        begin_implementation(self.root, ROOT, "sample-change", actor_id="implementer", context_id="impl-context", kind="agent")
        verify_change(self.root, ROOT, project, "sample-change", "development")
        verify_change(self.root, ROOT, project, "sample-change", "review")

        start_review(self.root, ROOT, "sample-change", actor_id="reviewer", context_id="review-context", kind="agent")
        review_doc_1 = {
            "schemaVersion": 7,
            "changeId": "sample-change",
            "reviewer": {"actorId": "reviewer", "contextId": "review-context", "kind": "agent"},
            "checkpoint": repository_snapshot(self.root),
            "verdict": "changes-requested",
            "summary": "First review requested changes.",
            "findings": [
                {
                    "id": "bug",
                    "severity": "blocking",
                    "priority": "P1",
                    "criterionId": "works",
                    "origin": "contract",
                    "summary": "Sample blocker.",
                }
            ],
            "productionEngineering": [
                {
                    "id": inv,
                    "status": "satisfied",
                    "rationale": "ok",
                    "evidence": ["test"],
                }
                for inv in invariant_ids
            ],
            "processImprovement": {"status": "none", "rationale": "none"},
        }
        r1_path = self.write(".process/runs/review-1.json", review_doc_1)
        submit_review(self.root, ROOT, "sample-change", r1_path)

        # Cycle 2 / Round 2: address finding, verify, approve, finish
        begin_implementation(self.root, ROOT, "sample-change", actor_id="implementer", context_id="impl-context", kind="agent")
        verify_change(self.root, ROOT, project, "sample-change", "development")
        verify_change(self.root, ROOT, project, "sample-change", "review")

        start_review(self.root, ROOT, "sample-change", actor_id="reviewer", context_id="review-context", kind="agent")
        review_doc_2 = {
            "schemaVersion": 7,
            "changeId": "sample-change",
            "reviewer": {"actorId": "reviewer", "contextId": "review-context", "kind": "agent"},
            "checkpoint": repository_snapshot(self.root),
            "verdict": "approved",
            "summary": "Second review approved.",
            "findings": [
                {
                    "id": "bug",
                    "severity": "non-blocking",
                    "priority": "P1",
                    "criterionId": "works",
                    "origin": "contract",
                    "summary": "Sample blocker resolved.",
                    "disposition": {"status": "resolved", "rationale": "Resolved in cycle 2."},
                },
                {
                    "id": "risk",
                    "severity": "non-blocking",
                    "priority": "P2",
                    "criterionId": "works",
                    "origin": "contract",
                    "summary": "Sample accepted risk.",
                    "disposition": {
                        "status": "accepted-risk",
                        "rationale": "Risk accepted.",
                        "owner": "lead",
                        "recordUrl": "https://example.com/risk/1",
                    },
                },
            ],
            "productionEngineering": [
                {
                    "id": inv,
                    "status": "satisfied",
                    "rationale": "ok",
                    "evidence": ["test"],
                }
                for inv in invariant_ids
            ],
            "processImprovement": {"status": "none", "rationale": "none"},
        }
        r2_path = self.write(".process/runs/review-2.json", review_doc_2)
        submit_review(self.root, ROOT, "sample-change", r2_path)

        state, receipt = finish_change(self.root, ROOT, "sample-change", actor_id="coordinator", context_id="finish-context", kind="agent")
        checkpoint = repository_snapshot(self.root)

        # AC1 & AC2: Factual field derivation from canonical run & receipt records
        data = build_pr_description_data(self.root, ROOT, "sample-change")
        self.assertEqual("https://example.com/issues/100", data["fields"]["source"])
        self.assertEqual("low", data["fields"]["risk"])
        self.assertEqual("`development`, `review`", data["fields"]["profiles"])
        self.assertEqual(f"`{checkpoint['fingerprint']}`", data["fields"]["snapshot"])
        self.assertEqual(f"`{digest_json(receipt)}`", data["fields"]["completion-receipt"])
        self.assertEqual("approved", data["fields"]["verdict"])
        # AC6: review rounds distinguished (2 review rounds recorded)
        self.assertEqual("2", data["fields"]["cycles"])
        self.assertEqual("0 open", data["fields"]["blocking-findings"])
        # AC6: carried/resolved finding and accepted risk with owner/recordUrl
        self.assertEqual(
            "bug: resolved; risk: accepted-risk (lead, https://example.com/risk/1)",
            data["fields"]["non-blocking-dispositions"],
        )
        self.assertEqual("pending", data["fields"]["outcome"])
        self.assertEqual("pending", data["fields"]["scope"])
        # AC6: contextual accepted-scope is not auto-approved
        self.assertFalse(data["checks"]["accepted-scope"])
        self.assertTrue(data["checks"]["required-profiles"])
        self.assertTrue(data["checks"]["independent-review"])
        self.assertTrue(data["checks"]["finding-dispositions"])

        # AC4: No reviewer actor/context ID, local run path, or secret enters public fields
        for field_id, value in data["fields"].items():
            self.assertNotIn("reviewer", value, f"reviewer actor leaked in {field_id}")
            self.assertNotIn("review-context", value, f"reviewer context leaked in {field_id}")
            self.assertNotIn(str(self.root), value, f"local path leaked in {field_id}")

        # AC5: Author overrides combined with canonical facts
        overrides = {
            "schemaVersion": 1,
            "fields": {
                "outcome": "Deliver bounded sample change.",
                "scope": "product.txt file only.",
                "verdict": "forged-verdict",  # Lifecycle-derived fact cannot be overwritten
            },
            "checks": {
                "accepted-scope": True,
            },
            "issueReference": "Refs #100.",
        }
        overrides_path = self.write(".process/runs/overrides.json", overrides)
        data_with_overrides = build_pr_description_data(self.root, ROOT, "sample-change", overrides=overrides)
        self.assertEqual("Deliver bounded sample change.", data_with_overrides["fields"]["outcome"])
        self.assertEqual("product.txt file only.", data_with_overrides["fields"]["scope"])
        self.assertEqual("approved", data_with_overrides["fields"]["verdict"])
        self.assertTrue(data_with_overrides["checks"]["accepted-scope"])
        self.assertEqual("Refs #100.", data_with_overrides["issueReference"])

        # AC2: Render and exact-byte validation with prepared data
        standard = resolve_standard(self.root, ROOT, "pull-request")
        body = render_description(standard, data_with_overrides, state="ready")
        self.assertEqual([], body_issues(body, "ready", standard))
        pr_path = self.root / ".process" / "runs" / "pr.md"
        pr_path.write_bytes(body.encode("utf-8"))

        # CLI prepare-pr-data
        prep_out = self.root / ".process" / "runs" / "prepared-pr.json"
        code, res = self.cli("prepare-pr-data", "--change-id", "sample-change", "--data-file", str(overrides_path), "--output", str(prep_out))
        self.assertEqual(0, code, res)
        self.assertTrue(prep_out.is_file())

        # CLI render with --change-id
        code, rendered_cli = self.cli("render", "--artifact", "pull-request", "--change-id", "sample-change", "--data-file", str(overrides_path))
        self.assertEqual(0, code, rendered_cli)
        self.assertEqual(body, rendered_cli["content"])

        # CLI validate with --change-id
        code, validated_cli = self.cli("validate", "--artifact", "pull-request", "--change-id", "sample-change", "--data-file", str(overrides_path), "--body-file", str(pr_path))
        self.assertEqual(0, code, validated_cli)

        # AC3: Stale candidate (mismatched checkpoint) cannot yield completed evidence claims
        (self.root / "product.txt").write_text("mutated\n", encoding="utf-8")
        stale_data = build_pr_description_data(self.root, ROOT, "sample-change", overrides=overrides)
        self.assertEqual("pending", stale_data["fields"]["snapshot"])
        self.assertEqual("pending", stale_data["fields"]["verdict"])
        self.assertEqual("pending", stale_data["fields"]["completion-receipt"])
        self.assertFalse(stale_data["checks"]["required-profiles"])
        self.assertFalse(stale_data["checks"]["independent-review"])
        self.assertFalse(stale_data["checks"]["finding-dispositions"])


if __name__ == "__main__":
    unittest.main()

