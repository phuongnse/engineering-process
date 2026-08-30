#!/usr/bin/env python3
"""Normalize an sdist tarball to one reproducible SOURCE_DATE_EPOCH."""

from __future__ import annotations

import argparse
from io import BytesIO
import gzip
import os
from pathlib import Path, PurePosixPath
import tarfile
import tempfile


MAX_MEMBERS = 10_000
MAX_TOTAL_BYTES = 100_000_000


def normalize(path: Path, epoch: int) -> None:
    members: list[tuple[tarfile.TarInfo, bytes | None]] = []
    total = 0
    with tarfile.open(path, "r:gz") as source:
        for member in source.getmembers():
            relative = PurePosixPath(member.name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or member.isdev()
                or len(members) >= MAX_MEMBERS
            ):
                raise RuntimeError(f"unsafe or excessive sdist member: {member.name}")
            content: bytes | None = None
            if member.isfile():
                stream = source.extractfile(member)
                if stream is None:
                    raise RuntimeError(f"cannot read sdist member: {member.name}")
                content = stream.read(MAX_TOTAL_BYTES - total + 1)
                total += len(content)
                if total > MAX_TOTAL_BYTES:
                    raise RuntimeError("sdist contents exceed the aggregate limit")
            members.append((member, content))

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.normalize-", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as raw:
            descriptor = -1
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw, mtime=epoch
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
                ) as target:
                    for member, content in sorted(
                        members, key=lambda item: item[0].name
                    ):
                        member.uid = 0
                        member.gid = 0
                        member.uname = ""
                        member.gname = ""
                        member.mtime = epoch
                        member.pax_headers = {
                            key: value
                            for key, value in member.pax_headers.items()
                            if key not in {"atime", "ctime", "mtime"}
                        }
                        target.addfile(
                            member,
                            BytesIO(content) if content is not None else None,
                        )
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("epoch", type=int)
    args = parser.parse_args()
    if args.epoch < 0:
        raise RuntimeError("SOURCE_DATE_EPOCH must be non-negative")
    normalize(args.path.resolve(strict=True), args.epoch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
