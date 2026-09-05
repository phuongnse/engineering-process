#!/usr/bin/env python3
"""Build, install, and exercise the exact wheel distribution."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from verification.normalize_sdist import normalize  # noqa: E402


def _utf8_lf(path: Path) -> str:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError(f"{path}: distribution text must be UTF-8 without BOM and use LF") from error
    if data.startswith(b"\xef\xbb\xbf") or b"\r" in data:
        raise RuntimeError(f"{path}: distribution text must be UTF-8 without BOM and use LF")
    return text


def validate_distribution_text(root: Path) -> None:
    metadata = tomllib.loads(_utf8_lf(root / "pyproject.toml"))
    paths = ["release.json", "engineering_process/__init__.py"]
    for declared in metadata["tool"]["setuptools"]["data-files"].values():
        paths.extend(declared)
    for relative in dict.fromkeys(paths):
        _utf8_lf(root / relative)


def run(
    command: list[str],
    *,
    cwd: Path,
    timeout: int = 300,
    environment: dict[str, str] | None = None,
) -> None:
    result = subprocess.run(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"command failed with exit {result.returncode}: {command}")


def main() -> int:
    validate_distribution_text(PROJECT_ROOT)
    with tempfile.TemporaryDirectory(prefix="engineering-process-dist-") as directory:
        root = Path(directory)
        artifacts = root / "dist"
        epoch = int(
            subprocess.check_output(
                ["git", "show", "-s", "--format=%ct", "HEAD"],
                cwd=PROJECT_ROOT,
                text=True,
            ).strip()
        )
        build_environment = {**os.environ, "SOURCE_DATE_EPOCH": str(epoch)}
        run(
            [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(artifacts)],
            cwd=PROJECT_ROOT,
            environment=build_environment,
        )
        wheels = list(artifacts.glob("*.whl"))
        sdists = list(artifacts.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise RuntimeError("build must produce exactly one wheel and one sdist")
        normalize(sdists[0], epoch)

        environment = root / "venv"
        run([sys.executable, "-m", "venv", str(environment)], cwd=root)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        processctl = environment / ("Scripts/processctl.exe" if os.name == "nt" else "bin/processctl")
        run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", str(wheels[0])],
            cwd=root,
        )
        run([str(processctl), "--version"], cwd=root, timeout=30)
        run([str(processctl), "skills", "validate", "--json"], cwd=root, timeout=30)
        run(
            [
                str(processctl),
                "publication",
                "validate-branch",
                "--branch",
                "fix/distribution-check",
                "--json",
            ],
            cwd=root,
            timeout=30,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"distribution verification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
