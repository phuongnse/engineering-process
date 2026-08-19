"""Private force-terminable HTTPS download worker."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time

from .contracts import ContractError, ManagedToolArtifact
from .tooling import _download_artifact_direct, _validated_https_target


_INPUT_LIMIT = 16_384


def _payload() -> tuple[ManagedToolArtifact, Path, float]:
    content = sys.stdin.buffer.read(_INPUT_LIMIT + 1)
    if len(content) > _INPUT_LIMIT:
        raise ContractError("download worker input exceeds its limit")
    try:
        value = json.loads(content.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"download worker input is invalid: {error}") from error
    if not isinstance(value, dict) or set(value) != {
        "destination",
        "maxDownloadBytes",
        "timeoutSeconds",
        "url",
    }:
        raise ContractError("download worker input has invalid fields")
    url = value["url"]
    maximum = value["maxDownloadBytes"]
    timeout = value["timeoutSeconds"]
    destination_value = value["destination"]
    if not isinstance(url, str):
        raise ContractError("download worker URL must be a string")
    _validated_https_target(url, url)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
        raise ContractError("download worker byte limit must be positive")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ContractError("download worker timeout must be positive")
    if not isinstance(destination_value, str) or "\x00" in destination_value:
        raise ContractError("download worker destination is invalid")
    destination = Path(destination_value)
    if not destination.is_absolute():
        raise ContractError("download worker destination must be absolute")
    artifact = ManagedToolArtifact(
        platform="worker",
        url=url,
        checksum="sha256:" + "0" * 64,
        archive_format="file",
        strip_components=0,
        max_download_bytes=maximum,
        max_extracted_bytes=maximum,
        max_files=1,
        commands={},
    )
    return artifact, destination, time.monotonic() + float(timeout)


def main() -> int:
    try:
        artifact, destination, deadline = _payload()
        _download_artifact_direct(artifact, destination, deadline=deadline)
        return 0
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
