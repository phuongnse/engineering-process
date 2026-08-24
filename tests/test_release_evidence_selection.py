import copy
import json
from pathlib import Path
import tempfile
import unittest

from engineering_process.contracts import ContractError
from verification.select_release_evidence import (
    MAX_EVIDENCE_BYTES,
    MAX_METADATA_BYTES,
    _bounded_document,
    select_release_evidence,
)


ACTIONS_ARTIFACT = "release-completion-" + "a" * 40
RELEASE_TAG = "v0.4.0"
EVIDENCE_ASSET = "engineering-process-v0.4.0-evidence.json"


def action_artifact(*, run_id=123, size=27_704, expired=False, created_at="2026-08-23T03:39:08Z"):
    return {
        "created_at": created_at,
        "expired": expired,
        "name": ACTIONS_ARTIFACT,
        "size_in_bytes": size,
        "workflow_run": {"id": run_id},
    }


def published_release():
    return {
        "assets": [
            {
                "digest": "sha256:" + "7" * 64,
                "name": EVIDENCE_ASSET,
                "size": 27_704,
                "state": "uploaded",
            }
        ],
        "immutabilityVerified": True,
        "isDraft": False,
        "name": RELEASE_TAG,
        "publishedAt": "2026-08-23T03:39:44Z",
        "tagName": RELEASE_TAG,
    }


def select(actions, release=None):
    return select_release_evidence(
        actions_document={"artifacts": actions},
        expected_actions_artifact=ACTIONS_ARTIFACT,
        expected_tag=RELEASE_TAG,
        evidence_asset=EVIDENCE_ASSET,
        release_document=release,
    )


class ReleaseEvidenceSelectionTests(unittest.TestCase):
    def test_primary_actions_artifact_wins_without_reading_release_state(self):
        selection = select(
            [
                action_artifact(run_id=100, created_at="2026-08-23T03:00:00Z"),
                action_artifact(run_id=200, created_at="2026-08-23T04:00:00Z"),
            ],
            release={"invalid": "must not be consulted"},
        )

        self.assertEqual("actions", selection["source"])
        self.assertEqual(200, selection["runId"])
        self.assertEqual(27_704, selection["size"])

    def test_expired_or_absent_primary_requires_published_release_metadata(self):
        for actions in ([], [action_artifact(expired=True)]):
            with self.subTest(actions=actions):
                self.assertEqual(
                    {"source": "published-release-required"},
                    select(actions),
                )

    def test_invalid_or_oversized_primary_fails_instead_of_falling_back(self):
        invalid = (
            action_artifact(size=MAX_EVIDENCE_BYTES + 1),
            action_artifact(size=True),
            {**action_artifact(), "expired": "false"},
            {**action_artifact(), "workflow_run": {"id": 0}},
            {**action_artifact(), "created_at": ""},
        )
        for artifact in invalid:
            with self.subTest(artifact=artifact):
                with self.assertRaises(ContractError):
                    select([artifact], release=published_release())

    def test_actions_metadata_count_is_bounded(self):
        with self.assertRaisesRegex(ContractError, "at most 100"):
            select_release_evidence(
                actions_document={"artifacts": [{} for _ in range(101)]},
                expected_actions_artifact=ACTIONS_ARTIFACT,
                expected_tag=RELEASE_TAG,
                evidence_asset=EVIDENCE_ASSET,
            )

    def test_exact_published_immutable_release_is_selected(self):
        selection = select([], release=published_release())

        self.assertEqual("published-release", selection["source"])
        self.assertEqual(RELEASE_TAG, selection["tag"])
        self.assertEqual(EVIDENCE_ASSET, selection["asset"])
        self.assertEqual(27_704, selection["size"])
        self.assertRegex(selection["digest"], r"^sha256:[0-9a-f]{64}$")

    def test_invalid_published_release_states_fail_closed(self):
        valid = published_release()
        duplicate = copy.deepcopy(valid)
        duplicate["assets"].append(copy.deepcopy(duplicate["assets"][0]))
        cases = {
            "absent": {},
            "draft": {**valid, "isDraft": True},
            "mismatched-name": {**valid, "name": "v0.4.1"},
            "mismatched-tag": {**valid, "tagName": "v0.4.1"},
            "missing-publication-time": {**valid, "publishedAt": None},
            "mutable": {**valid, "immutabilityVerified": False},
            "missing-evidence": {**valid, "assets": []},
            "duplicate-evidence": duplicate,
        }
        for label, release in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ContractError):
                    select([], release=release)

    def test_invalid_published_evidence_asset_fails_closed(self):
        valid = published_release()
        cases = {
            "incomplete": {"state": "new"},
            "empty": {"size": 0},
            "oversized": {"size": MAX_EVIDENCE_BYTES + 1},
            "boolean-size": {"size": True},
            "invalid-digest": {"digest": "sha256:invalid"},
        }
        for label, changes in cases.items():
            release = copy.deepcopy(valid)
            release["assets"][0].update(changes)
            with self.subTest(label=label):
                with self.assertRaises(ContractError):
                    select([], release=release)

    def test_remote_metadata_file_is_bounded_before_json_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            oversized = root / "oversized.json"
            oversized.write_bytes(b" " * (MAX_METADATA_BYTES + 1))
            with self.assertRaisesRegex(ContractError, "exceeds"):
                _bounded_document(oversized, label="remote metadata")

            valid = root / "valid.json"
            valid.write_text(json.dumps({"artifacts": []}), encoding="utf-8")
            self.assertEqual(
                {"artifacts": []},
                _bounded_document(valid, label="remote metadata"),
            )


if __name__ == "__main__":
    unittest.main()
