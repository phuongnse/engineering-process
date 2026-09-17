from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from engineering_process.contracts import ProcessError
from engineering_process.artifact_standards import resolve_standard
from engineering_process.pr_description import body_issues, validate_pull_request
from verification.generate_renovate_preset import generate_preset
from verification import generate_renovate_preset


ROOT = Path(__file__).resolve().parent.parent
ACTIVE_PROCESS_PIN = re.compile(
    r"^engineering-process==([^\s\\]+)\s*$", re.MULTILINE
)


class AutomationTests(unittest.TestCase):
    def test_renovate_preset_matches_the_canonical_pending_template(self) -> None:
        template = (ROOT / "templates" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
        preset = json.loads((ROOT / "templates" / "renovate.json").read_text(encoding="utf-8"))
        self.assertEqual(generate_preset(template), preset)
        standard = resolve_standard(None, ROOT, "pull-request")
        self.assertEqual([], body_issues(preset["prHeader"], "draft", standard))
        self.assertTrue(body_issues(preset["prHeader"], "ready", standard))

    def test_renovate_preset_rejects_an_incomplete_public_contract(self) -> None:
        template = (ROOT / "templates" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
        with self.assertRaises(ProcessError):
            generate_preset(template.replace("## Review and completion", "## Other"))

    def test_renovate_generator_rejects_changed_bytes_and_recovers(self) -> None:
        template = (ROOT / "templates" / "PULL_REQUEST_TEMPLATE.md").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "templates").mkdir()
            (root / "templates" / "PULL_REQUEST_TEMPLATE.md").write_bytes(template)
            target = root / "templates" / "renovate.json"
            with patch.object(generate_renovate_preset, "PROJECT_ROOT", root), patch("sys.argv", ["generate"]):
                self.assertEqual(0, generate_renovate_preset.main())
                expected = target.read_bytes()
                self.assertNotIn(b"\r", expected)
                self.assertFalse(expected.startswith(b"\xef\xbb\xbf"))
                for changed in (
                    expected.replace(b"\n", b"\r\n"),
                    expected.replace(b"\n", b"\r\n", 1),
                    b"\xef\xbb\xbf" + expected,
                ):
                    with self.subTest(changed=changed[:20]):
                        target.write_bytes(changed)
                        with patch("sys.argv", ["generate", "--check"]):
                            with self.assertRaisesRegex(ProcessError, "is stale"):
                                generate_renovate_preset.main()
                        self.assertEqual(0, generate_renovate_preset.main())
                        self.assertEqual(expected, target.read_bytes())
                with patch("sys.argv", ["generate", "--check"]):
                    self.assertEqual(0, generate_renovate_preset.main())

    def test_pull_request_template_defines_public_evidence_hierarchy(self) -> None:
        template = (ROOT / "templates" / "PULL_REQUEST_TEMPLATE.md").read_text(
            encoding="utf-8"
        )
        expected_sections = [
            "## Result",
            "## Contract and evidence",
            "## Review and completion",
        ]
        self.assertEqual(
            expected_sections,
            [line for line in template.splitlines() if line.startswith("## ")],
        )
        for field in (
            "Outcome",
            "Scope",
            "Source",
            "Risk",
            "Compatibility",
            "Profiles",
            "Snapshot",
            "Completion receipt",
            "Verdict",
            "Cycles",
            "Blocking findings",
            "Non-blocking dispositions",
        ):
            self.assertIn(f"- {field}:", template)
        self.assertIn("Keep reviewer actor/context IDs", template)
        self.assertNotIn("Record the independent reviewer", template)
        self.assertIn("accepted-risk", template)
        self.assertIn("tracked-follow-up", template)
        self.assertIn("## Review and completion", template)
        self.assertIn("Every non-blocking finding has a recorded disposition.", template)
        self.assertIn("Final verified consumer adoption only", template)
        self.assertIn("Closes ISSUE, closes OWNER/REPOSITORY#NUMBER", template)

    def test_renovate_process_rule_materializes_the_complete_adoption(self) -> None:
        config = json.loads((ROOT / ".github" / "renovate.json").read_text(encoding="utf-8"))
        rules = [
            rule
            for rule in config["packageRules"]
            if "engineering-process" in rule.get("matchPackageNames", [])
        ]
        self.assertEqual(1, len(rules))
        rule = rules[0]
        self.assertTrue(config["draftPR"])
        self.assertTrue(rule["enabled"])
        self.assertTrue(rule["draftPR"])
        self.assertEqual("automation/renovate/", config["branchPrefix"])
        self.assertEqual(
            ["python .process/adopt-process.py --project-root . --requirements-lock requirements/process.txt"],
            rule["postUpgradeTasks"]["commands"],
        )
        self.assertEqual("update", rule["postUpgradeTasks"]["executionMode"])
        self.assertEqual(
            [
                ".agents/skills/**",
                ".github/PULL_REQUEST_TEMPLATE.md",
                ".process/adopt-process.py",
                ".process/automation.json",
                ".process/adopt-process-windows-job.py",
                ".process/adoption-migrations/**",
                ".process/process.lock",
                ".process/project.json",
                "AGENTS.md",
            ],
            rule["postUpgradeTasks"]["fileFilters"],
        )

    def test_release_dispatches_the_published_version_to_the_control_plane(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "publish.yml").read_text(
            encoding="utf-8"
        )
        self.assertTrue(workflow.startswith("name: Publish release\n"))
        self.assertIn("engineering-process-published", workflow)
        self.assertIn("repos/phuongnse/renovate-ops/dispatches", workflow)
        self.assertIn("dispatch-adoption:", workflow)
        self.assertIn("  metadata:\n    name: Authorize release\n", workflow)
        self.assertIn("  publish:\n    name: Publish immutable release\n", workflow)
        self.assertIn(
            "  dispatch-adoption:\n    name: Dispatch consumer adoption\n", workflow
        )
        self.assertIn("needs: [metadata, publish]", workflow)
        dispatch_job = workflow.split("\n  dispatch-adoption:\n", maxsplit=1)[1]
        self.assertIn("    timeout-minutes: 20\n", dispatch_job)
        self.assertNotIn("wait_for_pypi_cache_horizon", dispatch_job)
        self.assertNotIn("published_at", dispatch_job)
        self.assertIn(
            "ref: ${{ github.sha }}",
            dispatch_job,
        )
        self.assertNotIn("ref: ${{ needs.metadata.outputs.source_sha }}", dispatch_job)
        self.assertLess(
            dispatch_job.index("      - name: Bind the published distribution\n"),
            dispatch_job.index("      - name: Create short-lived Renovate event token\n"),
        )
        self.assertIn("Verify PyPI exposes the exact built hashes", workflow)
        visibility = workflow.split(
            "      - name: Verify PyPI exposes the exact built hashes\n", maxsplit=1
        )[1].split("      - name: Create short-lived release token\n", maxsplit=1)[0]
        self.assertIn("for attempt in range(12)", visibility)
        self.assertIn('"Accept": "application/vnd.pypi.simple.v1+json"', visibility)
        self.assertIn('"Cache-Control": "max-age=0"', visibility)
        self.assertIn(
            "if actual == expected and simple_actual == expected:", visibility
        )
        self.assertIn("PyPI Simple API did not return PEP 691 JSON", visibility)
        self.assertIn("SOURCE_DATE_EPOCH", workflow)
        self.assertIn("verification/normalize_sdist.py", workflow)
        self.assertIn("points to a different commit", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("release_sha:", workflow)
        push_trigger = workflow.split("  workflow_dispatch:", maxsplit=1)[0]
        self.assertIn("      - release.json", push_trigger)
        self.assertNotIn("      - pyproject.toml", push_trigger)
        self.assertNotIn("      - engineering_process/__init__.py", push_trigger)
        self.assertIn(
            "for file in engineering_process/__init__.py pyproject.toml release.json; do",
            workflow,
        )
        self.assertIn('git merge-base --is-ancestor "$RELEASE_SOURCE_SHA" origin/main', workflow)
        self.assertIn("needs.metadata.outputs.source_sha", workflow)
        self.assertNotIn('--target "$GITHUB_SHA"', workflow)
        self.assertEqual(
            3,
            workflow.count(
                'path.suffix == ".whl" or path.name.endswith(".tar.gz")'
            ),
        )
        self.assertIn("for file in dist/*.whl dist/*.tar.gz; do", workflow)
        self.assertNotIn("for file in dist/*; do", workflow)
        publish_job = workflow.split("  publish:\n", maxsplit=1)[1].split(
            "\n  dispatch-adoption:\n", maxsplit=1
        )[0]
        self.assertIn("ref: ${{ needs.metadata.outputs.source_sha }}", publish_job)
        self.assertNotIn("ref: ${{ github.sha }}", publish_job)
        self.assertIn("id: release-token", publish_job)
        self.assertIn("repositories: engineering-process", publish_job)
        self.assertIn("permission-contents: write", publish_job)
        self.assertIn(
            "GH_TOKEN: ${{ steps.release-token.outputs.token }}", publish_job
        )
        self.assertNotIn("GH_TOKEN: ${{ github.token }}", publish_job)
        self.assertIn('"repos/$GITHUB_REPOSITORY/git/refs"', publish_job)
        self.assertIn('-f ref="refs/tags/$RELEASE_TAG"', publish_job)
        self.assertIn('-f sha="${{ needs.metadata.outputs.source_sha }}"', publish_job)
        self.assertIn("--verify-tag", publish_job)
        self.assertNotIn("--target", publish_job)
        trusted_checkout = workflow.index("          ref: main")
        preflight = workflow.index("name: Authorize release source from trusted main")
        source_checkout = workflow.index("ref: ${{ steps.release.outputs.source_sha }}")
        editable_install = workflow.index(
            "python -m pip install -r engineering_process/requirements-runtime.txt "
            "--editable ."
        )
        self.assertLess(trusted_checkout, preflight)
        self.assertLess(preflight, source_checkout)
        self.assertLess(source_checkout, editable_install)
        notes_check = workflow.index("python verification/render_release_notes.py --check RELEASE_NOTES.md")
        self.assertLess(notes_check, workflow.index("name: Publish immutable files to PyPI"))
        self.assertIn("notes=(--notes-file RELEASE_NOTES.md)", publish_job)
        self.assertIn("notes=(--generate-notes)", publish_job)
        self.assertIn('"${notes[@]}"', publish_job)
        self.assertLess(publish_job.index("GitHub release contents differ from the reviewed notes"), publish_job.index('gh release edit "$RELEASE_TAG" --draft=false'))
        self.assertNotIn("gh release edit \"$RELEASE_TAG\" --notes", publish_job)

    def test_release_pull_request_starts_and_refreshes_as_canonical_draft(self) -> None:
        body_path = ROOT / ".github" / "release-pr-body.md"
        result = validate_pull_request(
            title="chore(release): v1.2.0",
            branch="automation/release/v1.2.0",
            state="draft",
            body_path=body_path,
        )
        self.assertEqual([], result["issues"])
        body = body_path.read_text(encoding="utf-8")
        self.assertEqual(4, body.count("- [ ]"))

        workflow = (
            ROOT / ".github" / "workflows" / "release-pr.yml"
        ).read_text(encoding="utf-8")
        query = workflow.index('gh pr list --head "$branch" --state open')
        reset = workflow.index('gh pr ready "$pr_number" --undo')
        push = workflow.index('git push --force-with-lease origin "$branch"')
        edit = workflow.index('gh pr edit "$pr_number"')
        self.assertLess(query, reset)
        self.assertLess(reset, push)
        self.assertLess(push, edit)
        self.assertNotIn("2>/dev/null", workflow)
        self.assertEqual(3, workflow.count("--body-file .github/release-pr-body.md"))
        create = workflow.split("created_pr_url=\"$(gh pr create \\\n", maxsplit=1)[1]
        self.assertIn("              --draft \\\n", create)
        self.assertNotIn("--body \"Generated from", workflow)
        self.assertIn("release.json release-changes RELEASE_NOTES.md", workflow)
        self.assertIn("RELEASE_NOTES.md", body)

    def test_ci_checks_the_adopted_hash_locked_distribution_separately(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertTrue(workflow.startswith("name: CI\n"))
        events = workflow.split("on:\n", maxsplit=1)[1].split(
            "\npermissions:\n", maxsplit=1
        )[0]
        self.assertEqual(
            "  pull_request:\n"
            "    types: [opened, synchronize, reopened, ready_for_review, converted_to_draft, edited]\n"
            "  push:\n"
            "    branches: [main]\n",
            events,
        )
        self.assertIn("  checks: read\n", workflow)
        self.assertIn(
            "name: Verify (${{ matrix.os }}, Python ${{ matrix.python }})", workflow
        )
        policy_job = "  policy-verification:\n" + workflow.split(
            "  policy-verification:\n", maxsplit=1
        )[1].split("\n  adopted-process:\n", maxsplit=1)[0]
        self.assertEqual(
            "  policy-verification:\n"
            "    name: Policy verification\n"
            "    if: github.event_name == 'pull_request'\n"
            "    permissions:\n"
            "      contents: read\n"
            "      pull-requests: read\n"
            "    uses: phuongnse/renovate-ops/.github/workflows/"
            "policy-verification.yml@"
            "38d952b8c94604df10fadc48b6c830a144ea1137\n",
            policy_job,
        )
        self.assertIn(
            "  adopted-process:\n    name: Adopted public process\n", workflow
        )
        adopted_job = workflow.split("  adopted-process:\n", maxsplit=1)[1].split(
            "\n  test:\n", maxsplit=1
        )[0]
        self.assertIn("--require-hashes", adopted_job)
        self.assertIn("--refresh-package engineering-process", adopted_job)
        self.assertIn("Install exact producer dependencies for doctor", adopted_job)
        for requirements in (
            "engineering_process/requirements-runtime.txt",
            "engineering_process/requirements-dev.txt",
            "engineering_process/requirements-build.txt",
        ):
            self.assertIn(f"-r {requirements}", adopted_job)
        producer_install = adopted_job.index(
            "Install exact producer dependencies for doctor"
        )
        adoption_check = adopted_job.index("processctl adoption check")
        doctor = adopted_job.index("processctl doctor --project-root .")
        self.assertLess(producer_install, adoption_check)
        self.assertLess(adoption_check, doctor)
        publication = adopted_job.index("python verification/verify_publication.py --pull-request")
        self.assertLess(doctor, publication)
        self.assertIn("fetch-depth: 0", adopted_job)
        for field in ("head.ref", "title", "body", "draft", "base.sha", "head.sha"):
            self.assertIn(f"github.event.pull_request.{field}", adopted_job)
        self.assertNotIn("github.event.action", adopted_job)

        test_job = workflow.split("  test:\n", maxsplit=1)[1]
        self.assertNotIn("if:", test_job.split("runs-on:", maxsplit=1)[0])
        self.assertIn("Classify candidate verification", test_job)
        self.assertEqual(
            1,
            test_job.count('case "$GITHUB_EVENT_NAME:$PR_EVENT_ACTION:$PR_BASE_CHANGED"'),
        )
        self.assertEqual(1, test_job.count("cache: pip"))
        self.assertIn("if: steps.classify.outputs.mode == 'full'", test_job)
        self.assertIn("if: steps.classify.outputs.mode == 'retained'", test_job)
        self.assertIn("Verify retained code evidence for metadata-only update", test_job)
        self.assertIn("PR_EVENT_ACTION: ${{ github.event.action }}", test_job)
        self.assertIn("PR_BASE_CHANGED: ${{ github.event.changes.base && 'true' || 'false' }}", test_job)
        self.assertIn('check-runs?per_page=100', test_job)
        self.assertIn('item.get("head_sha") == head', test_job)
        self.assertIn('item.get("conclusion") == "success"', test_job)
        self.assertIn('item.get("status") == "completed"', test_job)
        self.assertIn('item.get("app") or {}', test_job)
        self.assertIn('PR_BASE_SHA: ${{ github.event.pull_request.base.sha }}', test_job)
        self.assertIn('base_marker = f"- Base: `{base}`"', test_job)
        self.assertIn('base_marker in ((item.get("output") or {}).get("summary") or "")', test_job)
        self.assertIn('Record code evidence boundary', test_job)
        self.assertIn('Record retained code evidence boundary', test_job)

        release_workflow = (
            ROOT / ".github" / "workflows" / "release-pr.yml"
        ).read_text(encoding="utf-8")
        self.assertTrue(release_workflow.startswith("name: Prepare release PR\n"))
        self.assertIn(
            "  prepare:\n    name: Prepare release pull request\n", release_workflow
        )
        self.assertIn("python processctl.py publication validate-pr", release_workflow)
        self.assertIn('python processctl.py publication validate-range', release_workflow)
        self.assertIn('edit_metadata=false', release_workflow)
        self.assertIn('if [ "$edit_metadata" = true ]; then', release_workflow)
        for field in ("baseRefName", "baseRefOid", "headRefName", "headRefOid"):
            self.assertIn(field, release_workflow)
        self.assertIn("expected_base=", release_workflow)
        self.assertIn("expected_head=", release_workflow)
        self.assertIn("jq -j '.body // \"\"'", release_workflow)
        self.assertIn('cmp -s "$body_path"', release_workflow)

    @staticmethod
    def _git_bash() -> str | None:
        if os.name == "nt":
            candidates = [
                Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                / "Git"
                / "bin"
                / "bash.exe",
                Path(r"C:\Program Files\Git\bin\bash.exe"),
            ]
            return next((str(path) for path in candidates if path.is_file()), None)
        return shutil.which("bash")

    def test_ci_classifier_executes_representative_event_fixtures(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        marker = "      - name: Classify candidate verification\n"
        start = workflow.index("        run: |\n", workflow.index(marker)) + len("        run: |\n")
        end = workflow.index("\n      - uses: actions/checkout", start)
        script = "\n".join(line[10:] for line in workflow[start:end].splitlines()) + "\n"
        bash = self._git_bash()
        if bash is None:
            self.skipTest("Git Bash is required to execute the workflow classifier")

        fixtures = (
            ("push", "", "false", "full"),
            ("pull_request", "opened", "false", "full"),
            ("pull_request", "synchronize", "false", "full"),
            ("pull_request", "reopened", "false", "full"),
            ("pull_request", "ready_for_review", "false", "full"),
            ("pull_request", "edited", "true", "full"),
            ("pull_request", "edited", "false", "retained"),
            ("pull_request", "converted_to_draft", "false", "retained"),
        )
        for event_name, action, base_changed, expected in fixtures:
            with self.subTest(event_name=event_name, action=action, base_changed=base_changed):
                with tempfile.TemporaryDirectory(dir=ROOT) as directory:
                    output = Path(directory) / "github-output"
                    output_value = output.as_posix()
                    if os.name == "nt":
                        output_value = f"/{output_value[0].lower()}{output_value[2:]}"
                    environment = os.environ.copy()
                    environment.update(
                        {
                            "GITHUB_EVENT_NAME": event_name,
                            "PR_EVENT_ACTION": action,
                            "PR_BASE_CHANGED": base_changed,
                            "GITHUB_OUTPUT": output_value,
                        }
                    )
                    result = subprocess.run(
                        [bash, "-c", script],
                        cwd=ROOT,
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertEqual(f"mode={expected}", output.read_text(encoding="utf-8").strip())

        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "github-output"
            output_value = output.as_posix()
            if os.name == "nt":
                output_value = f"/{output_value[0].lower()}{output_value[2:]}"
            environment = os.environ.copy()
            environment.update(
                {
                    "GITHUB_EVENT_NAME": "pull_request",
                    "PR_EVENT_ACTION": "unexpected",
                    "PR_BASE_CHANGED": "false",
                    "GITHUB_OUTPUT": output_value,
                }
            )
            result = subprocess.run(
                [bash, "-c", script],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("unsupported verification event", result.stderr)

    def test_ci_retained_check_filter_rejects_stale_or_failed_runs(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        marker = "      - name: Verify retained code evidence for metadata-only update\n"
        start = workflow.index("        run: |\n", workflow.index(marker)) + len("        run: |\n")
        end = workflow.index("\n      - name: Record retained code evidence boundary", start)
        script = "\n".join(line[10:] for line in workflow[start:end].splitlines()) + "\n"
        bash = self._git_bash()
        if bash is None:
            self.skipTest("Git Bash is required to execute the retained-check workflow step")

        head = "a" * 40
        base_one = "b" * 40
        base_two = "c" * 40
        check_name = "Verify (ubuntu-latest, Python 3.11)"
        payload = {"check_runs": []}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        server = HTTPServer(("127.0.0.1", 0), Handler)
        try:
            def run_step(
                records: list[dict[str, object]], base: str,
            ) -> subprocess.CompletedProcess[str]:
                payload["check_runs"] = records
                worker = threading.Thread(target=server.handle_request, daemon=True)
                worker.start()
                environment = os.environ.copy()
                environment.update(
                    {
                        "GITHUB_API_URL": f"http://127.0.0.1:{server.server_port}",
                        "GITHUB_REPOSITORY": "example/repository",
                        "GH_TOKEN": "test-token",
                        "PR_HEAD": head,
                        "PR_BASE_SHA": base,
                        "REQUIRED_CHECK_NAME": check_name,
                        "NO_PROXY": "127.0.0.1,localhost",
                        "no_proxy": "127.0.0.1,localhost",
                        "PATH": str(ROOT / ".venv" / "Scripts") + os.pathsep + os.environ["PATH"],
                    }
                )
                result = subprocess.run(
                    [bash, "-c", script],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive(), result.stderr + result.stdout)
                return result

            valid = {
                "id": 11,
                "name": check_name,
                "head_sha": head,
                "status": "completed",
                "conclusion": "success",
                "app": {"slug": "github-actions"},
            }
            valid["output"] = {"summary": f"Code evidence boundary\n- Base: `{base_one}`\n- Head: `{head}`\n"}
            stale = {**valid, "id": 12, "head_sha": "b" * 40}
            failed = {**valid, "id": 13, "conclusion": "failure"}
            wrong_app = {**valid, "id": 14, "app": {"slug": "other-app"}}
            accepted = run_step([stale, failed, wrong_app, valid], base_one)
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            self.assertIn("11", accepted.stdout)
            self.assertNotIn("12", accepted.stdout)

            rejected = run_step([valid], base_two)
            self.assertNotEqual(0, rejected.returncode)
            self.assertIn("No successful retained", rejected.stderr)

            failed_base_two = {**valid, "id": 15, "output": {"summary": f"Code evidence boundary\n- Base: `{base_two}`\n- Head: `{head}`\n"}, "conclusion": "failure"}
            missing_base_two = run_step([failed_base_two], base_two)
            self.assertNotEqual(0, missing_base_two.returncode)

            later_metadata = run_step([valid], base_two)
            self.assertNotEqual(0, later_metadata.returncode)

            valid_base_two = {**valid, "id": 16, "output": {"summary": f"Code evidence boundary\n- Base: `{base_two}`\n- Head: `{head}`\n"}}
            current_base = run_step([valid_base_two], base_two)
            self.assertEqual(0, current_base.returncode, current_base.stderr)
        finally:
            server.server_close()

    def test_release_metadata_compare_preserves_trailing_newlines(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release-pr.yml").read_text(
            encoding="utf-8"
        )
        marker = "          metadata_changed() {\n"
        start = workflow.index(marker)
        end = workflow.index("\n          existing_pr=", start)
        function = "\n".join(line[10:] for line in workflow[start:end].splitlines()) + "\n"
        bash = self._git_bash()
        if bash is None:
            self.skipTest("Git Bash is required to execute the release metadata comparison")

        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_jq = fake_bin / "jq"
            fake_jq.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$2\" == *'.title'* ]]; then\n"
                "  printf '%s\\n' \"$FAKE_TITLE\"\n"
                "elif [[ \"$2\" == *'.body'* ]]; then\n"
                "  cat \"$FAKE_BODY_FILE\"\n"
                "else\n"
                "  exit 1\n"
                "fi\n",
                encoding="utf-8",
                newline="",
            )
            os.chmod(fake_jq, 0o755)
            desired = root / "desired.md"
            current = root / "current.md"
            desired.write_bytes(b"release body\n")
            body_path = current.as_posix()
            if os.name == "nt":
                body_path = f"/{body_path[0].lower()}{body_path[2:]}"
            fake_bin_path = fake_bin.as_posix()
            if os.name == "nt":
                fake_bin_path = f"/{fake_bin_path[0].lower()}{fake_bin_path[2:]}"
            script = (
                "set -euo pipefail\n"
                + function
                + "if metadata_changed '{}' 'chore(release): v1.2.3' \"$DESIRED_BODY\"; then\n"
                + "  echo changed\n"
                + "else\n"
                + "  echo same\n"
                + "fi\n"
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": fake_bin_path + ":" + environment["PATH"],
                    "FAKE_TITLE": "chore(release): v1.2.3",
                    "FAKE_BODY_FILE": body_path,
                    "DESIRED_BODY": desired.as_posix()
                    if os.name != "nt"
                    else f"/{desired.as_posix()[0].lower()}{desired.as_posix()[2:]}",
                }
            )
            current.write_bytes(b"release body\n")
            same = subprocess.run(
                [bash, "-c", script], cwd=ROOT, env=environment,
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(0, same.returncode, same.stderr)
            self.assertEqual("same", same.stdout.strip())

            current.write_bytes(b"release body\n\n")
            changed = subprocess.run(
                [bash, "-c", script], cwd=ROOT, env=environment,
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(0, changed.returncode, changed.stderr)
            self.assertEqual("changed", changed.stdout.strip())

    def test_release_pr_binding_rejects_wrong_base_and_accepts_exact_candidate(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release-pr.yml").read_text(
            encoding="utf-8"
        )
        marker = "          validate_pr_binding() {\n"
        start = workflow.index(marker)
        end = workflow.index("\n          metadata_changed()", start)
        function = "\n".join(line[10:] for line in workflow[start:end].splitlines()) + "\n"
        bash = self._git_bash()
        if bash is None:
            self.skipTest("Git Bash is required to execute the release PR binding")

        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_jq = fake_bin / "jq"
            fake_jq.write_text(
                "#!/usr/bin/env bash\n"
                "case \"$2\" in\n"
                "  *.baseRefName*) printf '%s\\n' \"$FAKE_BASE_REF\" ;;\n"
                "  *.baseRefOid*) printf '%s\\n' \"$FAKE_BASE_OID\" ;;\n"
                "  *.headRefName*) printf '%s\\n' \"$FAKE_HEAD_REF\" ;;\n"
                "  *.headRefOid*) printf '%s\\n' \"$FAKE_HEAD_OID\" ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n",
                encoding="utf-8",
                newline="",
            )
            os.chmod(fake_jq, 0o755)
            fake_bin_path = fake_bin.as_posix()
            if os.name == "nt":
                fake_bin_path = f"/{fake_bin_path[0].lower()}{fake_bin_path[2:]}"
            branch = "automation/release/v1.2.3"
            base = "b" * 40
            head = "a" * 40
            script = (
                "set -euo pipefail\n"
                f"branch='{branch}'\n"
                f"expected_base='{base}'\n"
                + function
                + "validate_pr_binding '{}' '"
                + head
                + "'\n"
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": fake_bin_path + ":" + environment["PATH"],
                    "FAKE_BASE_REF": "release",
                    "FAKE_BASE_OID": base,
                    "FAKE_HEAD_REF": branch,
                    "FAKE_HEAD_OID": head,
                }
            )
            wrong_base = subprocess.run(
                [bash, "-c", script], cwd=ROOT, env=environment,
                capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(0, wrong_base.returncode)
            self.assertIn("does not bind the prepared base/head", wrong_base.stderr)

            environment["FAKE_BASE_REF"] = "main"
            exact = subprocess.run(
                [bash, "-c", script], cwd=ROOT, env=environment,
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(0, exact.returncode, exact.stderr)

    def test_readiness_sidecar_preserves_the_adopted_authority_bootstrap(self) -> None:
        project = json.loads((ROOT / ".process" / "project.json").read_text(encoding="utf-8"))
        readiness = json.loads((ROOT / ".process" / "readiness.json").read_text(encoding="utf-8"))
        self.assertNotIn("readiness", project)
        self.assertEqual("production", readiness["target"])
        self.assertEqual("production", readiness["stage"])
        self.assertEqual([{"id": "library-cli", "version": 1}], readiness["packs"])
        adopted = json.loads(
            (ROOT / ".process" / "process.lock").read_text(encoding="utf-8")
        )
        requirements = (ROOT / "requirements" / "process.in").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            [adopted["process"]["version"]], ACTIVE_PROCESS_PIN.findall(requirements)
        )
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("processctl adoption check", workflow)
        self.assertIn("processctl doctor --project-root .", workflow)

    def test_self_adoption_pin_parser_ignores_comments_and_prefix_collisions(self) -> None:
        self.assertEqual(
            ["1.0.10"], ACTIVE_PROCESS_PIN.findall("engineering-process==1.0.10\n")
        )
        self.assertEqual(
            [], ACTIVE_PROCESS_PIN.findall("# engineering-process==1.0.1\n")
        )

    def test_external_actions_are_immutably_pinned(self) -> None:
        for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
            for reference in re.findall(r"uses:\s*([^\s#]+)", workflow.read_text(encoding="utf-8")):
                if reference.startswith("./"):
                    continue
                self.assertRegex(reference, r"^[^@]+@[0-9a-f]{40}$", workflow.name)

    def test_consumer_improvement_issue_form_is_bounded_and_non_automated(self) -> None:
        form = (ROOT / ".github" / "ISSUE_TEMPLATE" / "consumer-process-improvement.yml").read_text(encoding="utf-8")
        self.assertEqual(
            {
                "authority",
                "blocking",
                "consumer",
                "disclosure",
                "evidence",
                "expected",
                "incident_type",
                "mitigation",
                "observed",
                "reusable",
                "stable_key",
            },
            set(re.findall(r"^    id: ([a-z_]+)$", form, re.MULTILINE)),
        )
        self.assertGreaterEqual(form.count("required: true"), 13)
        self.assertIn('title: "[consumer-process][CONSUMER-KEY][PROCESS-VERSION][INVARIANT][INCIDENT-KIND] "', form)
        self.assertIn("Search open and closed issues for this exact value", form)
        self.assertIn("phuongnse_2flyric-rail", form)
        self.assertIn("searched open and closed engineering-process issues for the complete stable key", form)
        for forbidden in ("secrets", "credentials", "raw private logs", "media", "private source"):
            self.assertIn(forbidden, form)
        workflows = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / ".github" / "workflows").glob("*.yml")
        )
        self.assertNotIn("consumer-process-improvement", workflows)


if __name__ == "__main__":
    unittest.main()
