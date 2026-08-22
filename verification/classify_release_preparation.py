from __future__ import annotations

import json
from pathlib import Path
import stat
import sys


MAX_RESULT_BYTES = 100_000
STALE_SELF_ADOPTION_RESULT = {
    "command": "publication",
    "errors": [
        "self-adoption must pin the latest public release before preparing another release"
    ],
    "status": "failed",
}


class ReleasePreparationError(RuntimeError):
    pass


def read_result(path: Path) -> object:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ReleasePreparationError("preparation result must be a regular file")
        if before.st_size > MAX_RESULT_BYTES:
            raise ReleasePreparationError("preparation result exceeds the size limit")
        with path.open("rb") as stream:
            content = stream.read(MAX_RESULT_BYTES + 1)
        after = path.lstat()
    except OSError as error:
        raise ReleasePreparationError(
            f"cannot read preparation result: {error}"
        ) from error
    if len(content) > MAX_RESULT_BYTES:
        raise ReleasePreparationError("preparation result exceeds the size limit")
    if (
        len(content) != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_mode != before.st_mode
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
    ):
        raise ReleasePreparationError("preparation result changed while reading")
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleasePreparationError(
            f"preparation result is invalid JSON: {error}"
        ) from error


def classify_result(document: object) -> str:
    if document != STALE_SELF_ADOPTION_RESULT:
        raise ReleasePreparationError(
            "preparation failure is not the exact stale self-adoption contract"
        )
    return "deferred-self-adoption"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: classify_release_preparation.py RESULT", file=sys.stderr)
        raise SystemExit(2)
    try:
        classification = classify_result(read_result(Path(sys.argv[1])))
    except ReleasePreparationError as error:
        print(f"release preparation classification: FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps({"classification": classification, "status": "passed"}))
