import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from engineering_process.contracts import canonical_json_digest, read_json
from verification.run_remote_verification import (
    AdapterError,
    _artifact_manifest,
    _selector_identity,
    verification_tag,
)
from verification.validate_remote_verification_dispatch import validate_dispatch
from verification.validate_remote_verification_dispatch import (
    BOOTSTRAP_AUTHORIZATION,
    BOOTSTRAP_BASE,
)


class RemoteVerificationAdapterTests(unittest.TestCase):
    def request(self) -> dict:
        return read_json(
            Path(__file__).resolve().parent.parent
            / "examples"
            / "remote-verification-request.json"
        )

    def initialize_repository(self, root: Path) -> tuple[str, str]:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "adapter@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Adapter Test"],
            cwd=root,
            check=True,
        )
        (root / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        (root / "tracked.txt").write_text("head\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "head"], cwd=root, check=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        return base, head

    def test_verification_tag_binds_request_identity(self):
        request = self.request()
        tag = verification_tag(request)
        self.assertEqual(
            (
                "epv/a9233c4908a3d579/c1/"
                f"{request['comparisonBase'][:16]}/{request['checkpoint']}/"
                f"{canonical_json_digest(request).removeprefix('sha256:')}"
            ),
            tag,
        )
        self.assertLessEqual(len(f"refs/tags/{tag}.lock"), 180)

    def test_dispatch_validator_requires_exact_tag_source_and_base_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, head = self.initialize_repository(root)
            request = self.request()
            request["checkpoint"] = head
            request["comparisonBase"] = base
            request["requirements"][0]["execution"]["workflowSha"] = base
            digest = canonical_json_digest(request)
            source_ref = f"refs/tags/{verification_tag(request)}"
            subprocess.run(
                ["git", "tag", source_ref.removeprefix("refs/tags/"), head],
                cwd=root,
                check=True,
            )
            result = validate_dispatch(
                root,
                source_ref=source_ref,
                change_id=request["changeId"],
                checkpoint=head,
                comparison_base=base,
                request_sha256=digest,
                expected_workflow_sha=base,
                actual_workflow_sha=base,
                event_name="workflow_dispatch",
            )
            self.assertEqual("passed", result["status"])
            with self.assertRaisesRegex(ValueError, "owned by the exact base"):
                validate_dispatch(
                    root,
                    source_ref=source_ref,
                    change_id=request["changeId"],
                    checkpoint=head,
                    comparison_base=base,
                    request_sha256=digest,
                    expected_workflow_sha=head,
                    actual_workflow_sha=head,
                    event_name="workflow_dispatch",
                )

    def test_artifact_manifest_and_selector_matching_are_bounded(self):
        request = self.request()
        manifest = {
            "platform": {"runnerOs": "Linux", "runnerArch": "X64"},
            "runtime": {"implementation": "CPython", "pythonVersion": "3.11.9"},
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
        parsed = _artifact_manifest(buffer.getvalue(), artifact_id=1)
        self.assertEqual(
            ("supported-python-platforms", "linux-python-3-11"),
            _selector_identity(request, parsed),
        )
        with self.assertRaisesRegex(AdapterError, "lacks manifest"):
            empty = io.BytesIO()
            with zipfile.ZipFile(empty, "w") as archive:
                archive.writestr("other.json", "{}")
            _artifact_manifest(empty.getvalue(), artifact_id=2)

    def test_bootstrap_workflow_exception_is_exactly_owner_scoped(self):
        head = "1" * 40
        request_digest = "sha256:" + "2" * 64
        source_ref = (
            "refs/tags/epv/f95b05a102367b6f/c1/"
            f"{BOOTSTRAP_BASE[:16]}/{head}/{request_digest.removeprefix('sha256:')}"
        )

        def git_result(_root, arguments):
            stdout = head + "\n" if arguments[0] == "rev-parse" else ""
            return subprocess.CompletedProcess(
                ["git", *arguments], 0, stdout=stdout, stderr=""
            )

        with mock.patch(
            "verification.validate_remote_verification_dispatch._git",
            side_effect=git_result,
        ):
            result = validate_dispatch(
                Path.cwd(),
                source_ref=source_ref,
                change_id="evidence-valid-remote-verification",
                checkpoint=head,
                comparison_base=BOOTSTRAP_BASE,
                request_sha256=request_digest,
                expected_workflow_sha=head,
                actual_workflow_sha=head,
                event_name="workflow_dispatch",
                bootstrap_authorization_sha256=BOOTSTRAP_AUTHORIZATION,
            )
            self.assertTrue(result["bootstrap"])
            with self.assertRaisesRegex(ValueError, "owned by the exact base"):
                validate_dispatch(
                    Path.cwd(),
                    source_ref=source_ref,
                    change_id="evidence-valid-remote-verification",
                    checkpoint=head,
                    comparison_base=BOOTSTRAP_BASE,
                    request_sha256=request_digest,
                    expected_workflow_sha=head,
                    actual_workflow_sha=head,
                    event_name="workflow_dispatch",
                    bootstrap_authorization_sha256="sha256:" + "3" * 64,
                )


if __name__ == "__main__":
    unittest.main()
