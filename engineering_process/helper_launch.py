"""Isolated launch commands for distribution-owned private helpers."""

from __future__ import annotations

from pathlib import Path
import sys


_HELPER_MODULES = {
    "engineering_process._download_worker",
    "engineering_process._windows_job",
}
_BOOTSTRAP = (
    "import runpy,sys;"
    "sys.path.insert(0,sys.argv.pop(1));"
    "runpy.run_module(sys.argv.pop(1),run_name='__main__',alter_sys=True)"
)


def isolated_helper_command(module: str, *arguments: str) -> tuple[str, ...]:
    """Build an argument array that cannot import a helper from the checkout."""

    if module not in _HELPER_MODULES:
        raise ValueError(f"untrusted private helper module: {module}")
    package_parent = Path(__file__).resolve(strict=True).parent.parent
    interpreter = Path(sys.executable)
    if not interpreter.is_absolute() or not interpreter.is_file():
        raise OSError("active Python interpreter path is unavailable")
    return (
        str(interpreter),
        "-I",
        "-c",
        _BOOTSTRAP,
        str(package_parent),
        module,
        *arguments,
    )
