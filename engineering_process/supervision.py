"""Portable foreground-task supervision.

The public execution contract is deliberately platform-neutral.  Kernel-specific
process containment lives in the selected backend and must not leak into consumer
manifests, reports, or lifecycle code.
"""

from __future__ import annotations

import os

from ._supervisor_contract import (
    CleanupOutcome,
    NATURAL_DRAIN_GRACE_MILLISECONDS,
    ProcessSupervisor,
    WINDOWS_NATURAL_DRAIN_GRACE_MILLISECONDS,
)


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
