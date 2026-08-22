from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import stat
import sys


MAX_EVENT_BYTES = 1_000_000
PAYLOAD_KEYS = {
    "attestationDigest",
    "commit",
    "repository",
    "tag",
    "version",
}
VERSION_PATTERN = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


class PublishEventError(RuntimeError):
    pass


def validate_publish_event(document: object) -> dict[str, str]:
    if not isinstance(document, dict):
        raise PublishEventError("repository dispatch event must be a JSON object")
    if document.get("action") != "engineering-process-release-ready":
        raise PublishEventError("unexpected repository dispatch action")
    repository = document.get("repository")
    sender = document.get("sender")
    if (
        not isinstance(repository, dict)
        or repository.get("full_name") != "phuongnse/engineering-process"
    ):
        raise PublishEventError("event targets an unexpected repository")
    if (
        not isinstance(sender, dict)
        or sender.get("login") != "phuongnse-renovate-ops[bot]"
    ):
        raise PublishEventError(
            "event sender is not the installed release GitHub App"
        )
    payload = document.get("client_payload")
    if not isinstance(payload, dict) or set(payload) != PAYLOAD_KEYS:
        raise PublishEventError("release event payload has unexpected fields")
    if payload.get("repository") != "phuongnse/engineering-process":
        raise PublishEventError("release event has an unexpected repository")
    version = payload.get("version")
    if (
        not isinstance(version, str)
        or len(version) > 64
        or VERSION_PATTERN.fullmatch(version) is None
    ):
        raise PublishEventError("release event has an invalid version")
    if payload.get("tag") != f"v{version}":
        raise PublishEventError("release event tag does not match its version")
    commit = payload.get("commit")
    if (
        not isinstance(commit, str)
        or len(commit) != 40
        or any(character not in "0123456789abcdef" for character in commit)
    ):
        raise PublishEventError("release event has an invalid commit")
    digest = payload.get("attestationDigest")
    if (
        not isinstance(digest, str)
        or len(digest) != 71
        or not digest.startswith("sha256:")
        or any(
            character not in "0123456789abcdef"
            for character in digest.removeprefix("sha256:")
        )
    ):
        raise PublishEventError(
            "release event has an invalid attestation digest"
        )
    return {key: payload[key] for key in sorted(PAYLOAD_KEYS)}


def read_publish_event(path: Path) -> dict[str, str]:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise PublishEventError("repository dispatch event must be a regular file")
        if before.st_size > MAX_EVENT_BYTES:
            raise PublishEventError("repository dispatch event exceeds the size limit")
        with path.open("rb") as stream:
            content = stream.read(MAX_EVENT_BYTES + 1)
        after = path.lstat()
    except OSError as error:
        raise PublishEventError(f"cannot read repository dispatch event: {error}") from error
    if len(content) > MAX_EVENT_BYTES:
        raise PublishEventError("repository dispatch event exceeds the size limit")
    if (
        len(content) != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_mode != before.st_mode
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
    ):
        raise PublishEventError("repository dispatch event changed while reading")
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublishEventError(
            f"repository dispatch event is invalid JSON: {error}"
        ) from error
    return validate_publish_event(document)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("event", type=Path)
    arguments = parser.parse_args()
    try:
        payload = read_publish_event(arguments.event)
    except Exception as error:
        print(f"publish event validation: FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps({"status": "passed", **payload}, indent=2, sort_keys=True))
