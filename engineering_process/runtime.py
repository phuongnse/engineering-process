from __future__ import annotations

import re
from importlib import metadata, resources

from .contracts import ContractError


PIN_PATTERN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s]+)$")


def runtime_dependency_pins() -> dict[str, str]:
    text = (
        resources.files("engineering_process")
        .joinpath("requirements-runtime.txt")
        .read_text(encoding="utf-8")
    )
    pins: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN_PATTERN.fullmatch(line)
        if match is None:
            raise ContractError(
                "engineering_process/requirements-runtime.txt:"
                f"{line_number}: runtime dependencies must use exact name==version pins"
            )
        name, version = match.groups()
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        if normalized in pins:
            raise ContractError(
                "engineering_process/requirements-runtime.txt: duplicate dependency "
                f"{normalized}"
            )
        pins[normalized] = version
    if not pins:
        raise ContractError("runtime dependency lock must not be empty")
    return pins


def assert_runtime_dependencies() -> None:
    mismatches: list[str] = []
    for name, expected in runtime_dependency_pins().items():
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            mismatches.append(f"{name} is missing; expected {expected}")
            continue
        if actual != expected:
            mismatches.append(f"{name} is {actual}; expected {expected}")
    if mismatches:
        raise ContractError(
            "engineering-process runtime dependency mismatch: " + "; ".join(mismatches)
        )
