import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from engineering_process.contracts import ContractError
from verification.restore_release_evidence import (
    MAX_EVIDENCE_BYTES,
    main,
    restore_release_evidence,
)


REVIEWED_SHA = "a" * 40
ARTIFACT_NAME = "release-completion-" + REVIEWED_SHA
TAG = "v0.4.0"
EVIDENCE_ASSET = "engineering-process-v0.4.0-evidence.json"
EVIDENCE = b"bounded lifecycle receipt\n"


def action_metadata(*, size=128):
    return {
        "artifacts": [
            {
                "created_at": "2026-08-23T03:39:08Z",
                "expired": False,
                "name": ARTIFACT_NAME,
                "size_in_bytes": size,
                "workflow_run": {"id": 123},
            }
        ]
    }


def release_metadata(*, size=len(EVIDENCE)):
    return {
        "assets": [
            {
                "digest": "sha256:" + "7" * 64,
                "name": EVIDENCE_ASSET,
                "size": size,
                "state": "uploaded",
            }
        ],
        "isDraft": False,
        "name": TAG,
        "publishedAt": "2026-08-23T03:39:44Z",
        "tagName": TAG,
    }


class FakeGitHubClient:
    def __init__(
        self,
        *,
        actions=None,
        release=None,
        verify_error=None,
        download_mode="valid",
    ):
        self.actions = {"artifacts": []} if actions is None else actions
        self.release = release_metadata() if release is None else release
        self.verify_error = verify_error
        self.download_mode = download_mode
        self.calls = []

    def actions_artifacts(self, *, artifact_name):
        self.calls.append(("actions-artifacts", artifact_name))
        return copy.deepcopy(self.actions)

    def published_release(self, *, tag):
        self.calls.append(("published-release", tag))
        return copy.deepcopy(self.release)

    def verify_release(self, *, tag):
        self.calls.append(("verify-release", tag))
        if self.verify_error is not None:
            raise self.verify_error

    def _materialize(self, output):
        path = output / EVIDENCE_ASSET
        if self.download_mode == "missing":
            return
        if self.download_mode == "directory":
            path.mkdir()
            return
        if self.download_mode == "oversized":
            with path.open("wb") as handle:
                handle.truncate(MAX_EVIDENCE_BYTES + 1)
            return
        if self.download_mode == "different-size":
            path.write_bytes(b"different\n")
            return
        path.write_bytes(EVIDENCE)

    def download_actions_artifact(self, *, run_id, artifact_name, output):
        self.calls.append(("download-actions", run_id, artifact_name))
        self._materialize(output)

    def download_release_asset(self, *, tag, asset_name, output):
        self.calls.append(("download-release", tag, asset_name))
        self._materialize(output)


def restore(client, output):
    return restore_release_evidence(
        client=client,
        reviewed_sha=REVIEWED_SHA,
        tag=TAG,
        evidence_asset=EVIDENCE_ASSET,
        output=output,
    )


class ReleaseEvidenceRestoreTests(unittest.TestCase):
    def test_primary_actions_artifact_restores_without_release_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            client = FakeGitHubClient(
                actions=action_metadata(),
                release={"invalid": "must not be consulted"},
            )

            selection = restore(client, output)

            self.assertEqual("actions", selection["source"])
            self.assertEqual(EVIDENCE, (output / EVIDENCE_ASSET).read_bytes())
            self.assertEqual(
                [
                    ("actions-artifacts", ARTIFACT_NAME),
                    ("download-actions", 123, ARTIFACT_NAME),
                ],
                client.calls,
            )

    def test_absent_primary_restores_only_verified_published_release(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            client = FakeGitHubClient()

            selection = restore(client, output)

            self.assertEqual("published-release", selection["source"])
            self.assertEqual(EVIDENCE, (output / EVIDENCE_ASSET).read_bytes())
            self.assertEqual(
                [
                    ("actions-artifacts", ARTIFACT_NAME),
                    ("published-release", TAG),
                    ("verify-release", TAG),
                    ("download-release", TAG, EVIDENCE_ASSET),
                ],
                client.calls,
            )

    def test_mutable_release_fails_before_download_and_cleans_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            client = FakeGitHubClient(
                verify_error=ContractError("release is not immutable")
            )

            with self.assertRaisesRegex(ContractError, "not immutable"):
                restore(client, output)

            self.assertFalse(output.exists())
            self.assertNotIn(
                ("download-release", TAG, EVIDENCE_ASSET), client.calls
            )

    def test_invalid_release_metadata_fails_before_download_and_cleans_output(self):
        valid = release_metadata()
        cases = {
            "absent": {},
            "draft": {**valid, "isDraft": True},
            "mismatch": {**valid, "tagName": "v0.4.1"},
            "missing-evidence": {**valid, "assets": []},
            "incomplete-evidence": {
                **valid,
                "assets": [{**valid["assets"][0], "state": "new"}],
            },
        }
        for label, release in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "evidence"
                client = FakeGitHubClient(release=release)
                with self.assertRaises(ContractError):
                    restore(client, output)
                self.assertFalse(output.exists())
                self.assertNotIn(
                    ("download-release", TAG, EVIDENCE_ASSET), client.calls
                )

    def test_invalid_downloaded_evidence_fails_closed_and_cleans_output(self):
        for mode in ("missing", "directory", "oversized", "different-size"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "evidence"
                client = FakeGitHubClient(download_mode=mode)
                with self.assertRaises(ContractError):
                    restore(client, output)
                self.assertFalse(output.exists())

    def test_existing_output_is_rejected_before_download(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            output.mkdir()
            client = FakeGitHubClient(actions=action_metadata())
            with self.assertRaisesRegex(ContractError, "must not already exist"):
                restore(client, output)
            self.assertNotIn(
                ("download-actions", 123, ARTIFACT_NAME), client.calls
            )

    def test_controller_cli_runs_with_a_historical_checkout_as_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            historical = Path(directory) / "historical-release"
            historical.mkdir()
            self.assertFalse(
                (historical / "verification" / "restore_release_evidence.py").exists()
            )
            output = Path(directory) / "evidence"
            client = FakeGitHubClient(actions=action_metadata())
            arguments = [
                "restore_release_evidence.py",
                "--repository",
                "phuongnse/engineering-process",
                "--reviewed-sha",
                REVIEWED_SHA,
                "--tag",
                TAG,
                "--evidence-asset",
                EVIDENCE_ASSET,
                "--output",
                str(output),
            ]
            previous = Path.cwd()
            try:
                os.chdir(historical)
                with (
                    mock.patch(
                        "verification.restore_release_evidence.CliGitHubClient",
                        return_value=client,
                    ),
                    mock.patch("sys.argv", arguments),
                    contextlib.redirect_stdout(io.StringIO()) as stdout,
                ):
                    self.assertEqual(0, main())
            finally:
                os.chdir(previous)
            self.assertEqual("actions", json.loads(stdout.getvalue())["source"])
            self.assertEqual(EVIDENCE, (output / EVIDENCE_ASSET).read_bytes())


if __name__ == "__main__":
    unittest.main()
