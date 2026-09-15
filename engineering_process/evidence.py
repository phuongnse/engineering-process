"""Shared runtime and lifecycle evidence identity helpers."""

from __future__ import annotations

from importlib import metadata
import hashlib
import os
import platform
from pathlib import Path
import sys
from typing import Any, Mapping

from . import VERSION
from .contracts import digest_json
from .distribution import distribution_digest
from .repository import same_checkpoint


SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "PRIVATE_KEY")


def child_environment(
    *,
    executable: str | Path | None = None,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Project the exact sanitized environment used by consumer checks."""
    environment: dict[str, str] = {}
    for name, value in (os.environ if source is None else source).items():
        upper = name.upper()
        if name in {"PYTHONHOME", "PYTHONPATH"}:
            continue
        if any(marker in upper for marker in SECRET_MARKERS):
            continue
        # Empty bindings remain meaningful inputs to a consumer command.
        environment[name] = value
    runtime_executable = Path(
        sys.executable if executable is None else executable
    ).absolute()
    runtime_directory = str(runtime_executable.parent)
    inherited_path = environment.get("PATH", "")
    path_entries = [entry for entry in inherited_path.split(os.pathsep) if entry]
    runtime_is_first = bool(path_entries) and (
        os.path.normcase(os.path.normpath(path_entries[0]))
        == os.path.normcase(os.path.normpath(runtime_directory))
    )
    if not runtime_is_first:
        path_entries.insert(0, runtime_directory)
    environment["PATH"] = os.pathsep.join(path_entries)
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def execution_identity(
    *,
    executable: str | Path | None = None,
    source: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return the runtime inputs used to launch consumer checks."""
    try:
        # Keep duplicate distribution records: multiplicity is part of the
        # environment identity, even when records have the same display name.
        dependencies = sorted(
            f"{distribution.name}=={distribution.version}@"
            f"{Path(distribution.locate_file('')).resolve()}"
            for distribution in metadata.distributions(path=_metadata_search_path())
        )
    except Exception:
        dependency_identity: dict[str, Any] = {"known": False}
    else:
        dependency_identity = {
            "known": True,
            "count": len(dependencies),
            "digest": "sha256:" + hashlib.sha256(
                "\n".join(dependencies).encode("utf-8")
            ).hexdigest(),
        }
    runtime_executable = Path(
        sys.executable if executable is None else executable
    ).absolute()
    return {
        "executable": str(runtime_executable),
        "python": sys.version,
        "platform": platform.platform(),
        "environment": child_environment(
            executable=runtime_executable, source=source
        ),
        "dependencies": dependency_identity,
    }


def _metadata_search_path() -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for entry in sys.path:
        value = os.fspath(entry) if entry else os.curdir
        key = os.path.normcase(str(Path(value).resolve()))
        if key in seen:
            continue
        seen.add(key)
        paths.append(value)
    return paths


def verification_input_digest(
    project_root: Path,
    process_root: Path,
    project: dict[str, Any],
    state: dict[str, Any],
    profile: str,
    *,
    runtime: dict[str, Any] | None = None,
) -> str | None:
    """Bind reusable evidence to every input controlled by this process."""
    runtime = runtime if runtime is not None else execution_identity()
    dependencies = runtime.get("dependencies")
    if not isinstance(dependencies, Mapping) or dependencies.get("known") is not True:
        return None
    return digest_json({
        "authority": {
            "version": VERSION,
            "distribution": distribution_digest(process_root),
        },
        "project": project,
        "contractDigest": state["contract"]["digest"],
        "planDigest": state["plan"]["digest"] if state.get("plan") else None,
        "comparisonBaseCommit": state.get("comparisonBaseCommit"),
        "profile": profile,
        "projectRoot": str(project_root.resolve()),
        "runtime": runtime,
        "host": platform.node(),
        "pythonImplementation": sys.implementation.name,
    })


def verification_report_matches_inputs(
    project_root: Path,
    process_root: Path,
    project: dict[str, Any],
    state: dict[str, Any],
    profile: str,
    report: dict[str, Any],
    checkpoint: dict[str, Any],
    *,
    require_input: bool,
    require_explicit_scope: bool = False,
    runtime: dict[str, Any] | None = None,
) -> bool:
    """Return whether one report is reusable for the supplied inputs."""
    scope = report.get("scope")
    if scope is None:
        if require_explicit_scope:
            return False
        scope = {"kind": "profile"}
    if (
        report.get("profile") != profile
        or report.get("status") != "passed"
        or scope != {"kind": "profile"}
        or not same_checkpoint(report.get("checkpoint", {}), checkpoint)
    ):
        return False
    recorded = report.get("inputDigest")
    if recorded is None:
        return not require_input
    current = verification_input_digest(
        project_root,
        process_root,
        project,
        state,
        profile,
        runtime=runtime,
    )
    return current is not None and recorded == current
