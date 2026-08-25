"""Portable foreground-task supervision.

The public execution contract is deliberately platform-neutral.  Kernel-specific
process containment lives in the selected backend and must not leak into consumer
manifests, reports, or lifecycle code.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Mapping, Protocol


NATURAL_DRAIN_GRACE_MILLISECONDS = 250
WINDOWS_NATURAL_DRAIN_GRACE_MILLISECONDS = 5_000


@dataclass(frozen=True)
class CleanupOutcome:
    """Result of a bounded process-tree cleanup operation."""

    bounded: bool
    descendants_found: bool = False
    error: str | None = None


class ProcessSupervisor(Protocol):
    """OS backend for one finite, non-interactive foreground task."""

    def resolve_application(
        self,
        command: str,
        *,
        working_directory: Path,
        environment: Mapping[str, str],
    ) -> Path: ...

    def spawn(
        self,
        command: tuple[str, ...],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        pipe_stdin: bool = False,
    ) -> subprocess.Popen[bytes]: ...

    def terminate(
        self,
        process: subprocess.Popen[bytes],
        *,
        grace_seconds: float,
    ) -> CleanupOutcome: ...

    def finalize(
        self,
        process: subprocess.Popen[bytes],
        *,
        grace_seconds: float,
    ) -> CleanupOutcome: ...


def process_supervisor(*, platform_name: str | None = None) -> ProcessSupervisor:
    """Select the sole platform adapter used by the execution layer."""

    selected = os.name if platform_name is None else platform_name
    if selected == "posix":
        from ._supervisor_posix import POSIX_SUPERVISOR

        return POSIX_SUPERVISOR
    if selected == "nt":
        from ._supervisor_windows import WINDOWS_SUPERVISOR

        return WINDOWS_SUPERVISOR
    raise OSError(f"unsupported process supervision platform: {selected}")
