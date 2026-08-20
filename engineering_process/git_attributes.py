from __future__ import annotations

import os
import stat
from pathlib import Path

from .contracts import ContractError


ATTRIBUTES_START = "# engineering-process:attributes:start"
ATTRIBUTES_END = "# engineering-process:attributes:end"
BYTE_STABLE_ATTRIBUTES = "text=auto eol=lf -working-tree-encoding -filter -ident"
MANAGED_ATTRIBUTES_SELF = f".gitattributes {BYTE_STABLE_ATTRIBUTES}"
MANAGED_SKILLS_ATTRIBUTES = f"skills/** {BYTE_STABLE_ATTRIBUTES}"
ATTRIBUTES_INPUT_LIMIT = 4_096


def _normalized_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def canonical_attributes_block() -> str:
    return "\n".join(
        (
            ATTRIBUTES_START,
            MANAGED_ATTRIBUTES_SELF,
            MANAGED_SKILLS_ATTRIBUTES,
            ATTRIBUTES_END,
        )
    ) + "\n"


def read_managed_attributes(path: Path) -> str | None:
    if path.is_symlink():
        raise ContractError("managed Git attributes must not be a symlink")
    if not os.path.lexists(path):
        return None
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ContractError(f"cannot read managed Git attributes: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ContractError("managed Git attributes must be a regular file")
        if metadata.st_size > ATTRIBUTES_INPUT_LIMIT:
            raise ContractError(
                "managed Git attributes exceed "
                f"{ATTRIBUTES_INPUT_LIMIT} bytes"
            )
        data = os.read(descriptor, ATTRIBUTES_INPUT_LIMIT + 1)
    finally:
        os.close(descriptor)
    if len(data) > ATTRIBUTES_INPUT_LIMIT:
        raise ContractError(
            "managed Git attributes exceed "
            f"{ATTRIBUTES_INPUT_LIMIT} bytes"
        )
    try:
        return data.decode("utf-8")
    except UnicodeError as error:
        raise ContractError(
            f"managed Git attributes are not valid UTF-8: {error}"
        ) from error


def has_managed_attributes_marker(text: str) -> bool:
    lines = _normalized_text(text).splitlines()
    return ATTRIBUTES_START in lines or ATTRIBUTES_END in lines


def managed_attributes_issues(text: str) -> list[str]:
    if _normalized_text(text) != canonical_attributes_block():
        return ["managed Git attributes differ from the pinned distribution"]
    return []
