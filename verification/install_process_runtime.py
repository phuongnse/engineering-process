from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


PUBLIC_INDEX = "https://pypi.org/simple"
MAX_LOCK_BYTES = 1_000_000
MAX_OUTPUT_BYTES = 1_000_000
ATTEMPT_TIMEOUT_SECONDS = 300
BACKOFF_SECONDS = (10, 20, 40, 80, 160)
PIN_PATTERN = re.compile(
    r"(?m)^engineering-process=="
    r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:[a-z0-9.-]+)?)(?:[ \t]+\\)?$"
)


class InstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class Attempt:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_exceeded: bool = False


AttemptRunner = Callable[[Sequence[str], Path, dict[str, str]], Attempt]


def _contained_lock(project_root: Path, requirements_lock: Path) -> Path:
    lexical_root = Path(os.path.abspath(project_root))
    root = project_root.resolve(strict=True)
    candidate = requirements_lock
    if not candidate.is_absolute():
        candidate = lexical_root / candidate
    else:
        candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(lexical_root)
    except ValueError as error:
        raise InstallError("requirements lock escapes the project root") from error
    current = lexical_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise InstallError(f"requirements lock path must not contain symlinks: {current}")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise InstallError(f"cannot resolve requirements lock: {error}") from error
    if not resolved.is_file():
        raise InstallError("requirements lock must be a regular file")
    return resolved


def _read_pin(lock_path: Path) -> str:
    try:
        if lock_path.stat().st_size > MAX_LOCK_BYTES:
            raise InstallError("requirements lock exceeds the size limit")
        content = lock_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise InstallError(f"cannot read requirements lock: {error}") from error
    matches = list(PIN_PATTERN.finditer(content))
    if len(matches) != 1:
        raise InstallError(
            "requirements lock must contain exactly one exact engineering-process pin"
        )
    if "--only-binary :all:" not in content:
        raise InstallError("requirements lock must enforce --only-binary :all:")
    return matches[0].group("version")


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "PIP_EXTRA_INDEX_URL",
        "PIP_INDEX_URL",
        "PIP_TRUSTED_HOST",
        "PYTHONHOME",
        "PYTHONPATH",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
        }
    )
    return environment


def pip_command(
    lock_path: Path, python_executable: str | Path = sys.executable
) -> tuple[str, ...]:
    return (
        str(python_executable),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--no-cache-dir",
        "--index-url",
        PUBLIC_INDEX,
        "--require-hashes",
        "-r",
        str(lock_path),
    )


def _run_attempt(
    command: Sequence[str], working_directory: Path, environment: dict[str, str]
) -> Attempt:
    with tempfile.TemporaryDirectory(prefix="process-runtime-install-") as directory:
        output_root = Path(directory)
        stdout_path = output_root / "stdout"
        stderr_path = output_root / "stderr"
        try:
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                result = subprocess.run(
                    list(command),
                    cwd=working_directory,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    check=False,
                    timeout=ATTEMPT_TIMEOUT_SECONDS,
                )
        except subprocess.TimeoutExpired:
            return Attempt(-1, b"", b"", timed_out=True)
        except OSError as error:
            raise InstallError(f"cannot execute pip: {error}") from error
        stdout_size = stdout_path.stat().st_size
        stderr_size = stderr_path.stat().st_size
        if stdout_size + stderr_size > MAX_OUTPUT_BYTES:
            return Attempt(
                result.returncode,
                b"",
                b"",
                output_exceeded=True,
            )
        return Attempt(
            result.returncode,
            stdout_path.read_bytes(),
            stderr_path.read_bytes(),
        )


def retryable_exact_version_absence(attempt: Attempt, version: str) -> bool:
    if (
        attempt.returncode == 0
        or attempt.timed_out
        or attempt.output_exceeded
    ):
        return False
    output = (attempt.stdout + b"\n" + attempt.stderr).decode(
        "utf-8", errors="replace"
    )
    requirement = f"engineering-process=={version}"
    return (
        f"Could not find a version that satisfies the requirement {requirement}"
        in output
        and f"No matching distribution found for {requirement}" in output
    )


def install_process_runtime(
    project_root: Path,
    requirements_lock: Path,
    *,
    python_executable: str | Path = sys.executable,
    runner: AttemptRunner = _run_attempt,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    root = project_root.resolve(strict=True)
    lock_path = _contained_lock(project_root, requirements_lock)
    version = _read_pin(lock_path)
    executable = Path(python_executable)
    if not executable.is_file():
        raise InstallError("Python executable must be an existing file")
    command = pip_command(lock_path, executable)
    environment = _environment()
    total_attempts = len(BACKOFF_SECONDS) + 1
    for index in range(total_attempts):
        attempt_number = index + 1
        print(
            f"Installing engineering-process {version} "
            f"(attempt {attempt_number}/{total_attempts})",
            flush=True,
        )
        attempt = runner(command, root, environment)
        if attempt.stdout:
            sys.stdout.buffer.write(attempt.stdout)
            sys.stdout.buffer.flush()
        if attempt.stderr:
            sys.stderr.buffer.write(attempt.stderr)
            sys.stderr.buffer.flush()
        if attempt.timed_out:
            raise InstallError(
                f"pip attempt exceeded {ATTEMPT_TIMEOUT_SECONDS} seconds"
            )
        if attempt.output_exceeded:
            raise InstallError(
                f"pip attempt output exceeded {MAX_OUTPUT_BYTES} bytes"
            )
        if attempt.returncode == 0:
            return
        if not retryable_exact_version_absence(attempt, version):
            raise InstallError(
                f"pip failed with non-retryable exit code {attempt.returncode}"
            )
        if index == len(BACKOFF_SECONDS):
            raise InstallError(
                f"engineering-process {version} did not become visible "
                f"after {total_attempts} attempts"
            )
        delay = BACKOFF_SECONDS[index]
        print(
            f"Exact public version is not visible yet; retrying in {delay} seconds.",
            flush=True,
        )
        sleeper(delay)
    raise AssertionError("unreachable install loop")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--requirements-lock",
        type=Path,
        default=Path("requirements/process.txt"),
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    arguments = parser.parse_args()
    try:
        install_process_runtime(
            arguments.project_root,
            arguments.requirements_lock,
            python_executable=arguments.python,
        )
    except (InstallError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
