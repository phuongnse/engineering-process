"""Platform-neutral process supervision contract."""

from __future__ import annotations

from dataclasses import dataclass
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

    def observe(self, process: subprocess.Popen[bytes]) -> None: ...

    def finalize(
        self,
        process: subprocess.Popen[bytes],
        *,
        grace_seconds: float,
    ) -> CleanupOutcome: ...
