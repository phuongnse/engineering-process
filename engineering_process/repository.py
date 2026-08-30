"""Read-only repository snapshots used to invalidate stale evidence."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import subprocess
from typing import Any

from .contracts import ProcessError


MAX_FILES = 100_000
MAX_FILE_BYTES = 100_000_000
MAX_TOTAL_BYTES = 1_000_000_000
STATE_PREFIXES = (b".process/runs/", b".process/receipts/")


def _git(root: Path, arguments: list[str], *, check: bool = True) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotepath=false", *arguments],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProcessError(f"cannot inspect Git repository: {error}") from error
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ProcessError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout


def _head(root: Path) -> str | None:
    value = _git(root, ["rev-parse", "--verify", "HEAD"], check=False).strip()
    if not value:
        return None
    try:
        decoded = value.decode("ascii")
    except UnicodeDecodeError as error:
        raise ProcessError("Git returned a non-ASCII HEAD") from error
    if len(decoded) not in {40, 64} or any(c not in "0123456789abcdef" for c in decoded):
        raise ProcessError("Git returned an invalid HEAD")
    return decoded


def repository_snapshot(root: Path) -> dict[str, Any]:
    root = root.resolve()
    if not (root / ".git").exists():
        # Linked worktrees have a .git file, so exists() is intentionally used.
        raise ProcessError(f"{root}: lifecycle evidence requires a Git repository")

    raw_paths = _git(
        root,
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
    ).split(b"\0")
    paths = sorted(
        path
        for path in raw_paths
        if path and not any(path.startswith(prefix) for prefix in STATE_PREFIXES)
    )
    if len(paths) > MAX_FILES:
        raise ProcessError(f"repository snapshot exceeds {MAX_FILES} files")

    digest = hashlib.sha256()
    total = 0
    for raw_path in paths:
        relative = os.fsdecode(raw_path)
        path = root / relative
        mode = 0
        try:
            info = path.lstat()
        except FileNotFoundError:
            kind = b"missing"
            data_digest = hashlib.sha256(b"").digest()
            size = 0
        except OSError as error:
            raise ProcessError(f"cannot inspect {relative}: {error}") from error
        else:
            mode = info.st_mode & 0o777
            if stat.S_ISLNK(info.st_mode):
                kind = b"symlink"
                target = os.fsencode(os.readlink(path))
                size = len(target)
                data_digest = hashlib.sha256(target).digest()
            elif stat.S_ISREG(info.st_mode):
                kind = b"file"
                size = info.st_size
                if size > MAX_FILE_BYTES:
                    raise ProcessError(
                        f"{relative}: snapshot file exceeds {MAX_FILE_BYTES} bytes"
                    )
                file_digest = hashlib.sha256()
                try:
                    with path.open("rb") as stream:
                        while chunk := stream.read(1024 * 1024):
                            file_digest.update(chunk)
                except OSError as error:
                    raise ProcessError(f"cannot read {relative}: {error}") from error
                data_digest = file_digest.digest()
            elif stat.S_ISDIR(info.st_mode):
                kind = b"directory"
                submodule_head = _git(
                    path, ["rev-parse", "--verify", "HEAD"], check=False
                ).strip()
                size = len(submodule_head)
                data_digest = hashlib.sha256(submodule_head).digest()
            else:
                raise ProcessError(f"{relative}: unsupported repository file type")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ProcessError(
                f"repository snapshot exceeds {MAX_TOTAL_BYTES} aggregate bytes"
            )
        digest.update(len(raw_path).to_bytes(4, "big"))
        digest.update(raw_path)
        digest.update(kind)
        digest.update(mode.to_bytes(2, "big"))
        digest.update(size.to_bytes(8, "big"))
        digest.update(data_digest)

    return {
        "head": _head(root),
        "fingerprint": "sha256:" + digest.hexdigest(),
        "fileCount": len(paths),
        "byteCount": total,
    }


def same_checkpoint(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left.get("head") == right.get("head")
        and left.get("fingerprint") == right.get("fingerprint")
    )
