# Managed by engineering-process; do not edit.
from __future__ import annotations

import argparse
import hashlib
import os
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path


COMMAND_TIMEOUT_SECONDS = 300
TERMINATION_TIMEOUT_SECONDS = 5
MAX_CAPTURE_BYTES = 128_000
READ_CHUNK_BYTES = 64 * 1024


@dataclass
class Capture:
    content: bytearray = field(default_factory=bytearray)
    count: int = 0
    digest: object = field(default_factory=hashlib.sha256)

    def add(self, chunk: bytes) -> None:
        self.count += len(chunk)
        self.digest.update(chunk)
        remaining = MAX_CAPTURE_BYTES - len(self.content)
        if remaining > 0:
            self.content.extend(chunk[:remaining])

    def text(self) -> str:
        value = bytes(self.content).decode("utf-8", errors="replace")
        if self.count > len(self.content):
            value += (
                f"\n[output truncated: {self.count} bytes, "
                f"sha256:{self.digest.hexdigest()}]\n"
            )
        return value


def _drain(stream: object, capture: Capture) -> None:
    try:
        while chunk := stream.read(READ_CHUNK_BYTES):
            capture.add(chunk)
    finally:
        stream.close()


def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=TERMINATION_TIMEOUT_SECONDS,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=TERMINATION_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=TERMINATION_TIMEOUT_SECONDS)


def _child_environment() -> dict[str, str]:
    allowed = {
        "LANG",
        "LC_ALL",
        "PATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    return environment


def _run(argv: list[str], *, cwd: Path) -> str:
    options: dict[str, object] = {
        "cwd": cwd,
        "env": _child_environment(),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(argv, **options)
    stdout = Capture()
    stderr = Capture()
    stdout_thread = threading.Thread(
        target=_drain, args=(process.stdout, stdout), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_drain, args=(process.stderr, stderr), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        return_code = process.wait(timeout=COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        _terminate_tree(process)
        raise RuntimeError(
            f"command timed out after {COMMAND_TIMEOUT_SECONDS} seconds"
        ) from error
    except BaseException:
        _terminate_tree(process)
        raise
    finally:
        stdout_thread.join(timeout=TERMINATION_TIMEOUT_SECONDS)
        stderr_thread.join(timeout=TERMINATION_TIMEOUT_SECONDS)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        _terminate_tree(process)
        raise RuntimeError("command output readers did not terminate")
    if return_code != 0:
        raise RuntimeError(
            f"command failed with exit status {return_code}\n"
            f"stdout:\n{stdout.text()}\nstderr:\n{stderr.text()}"
        )
    return stdout.text()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Materialize one hash-locked engineering-process adoption"
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--requirements-lock",
        type=Path,
        default=Path("requirements/process.txt"),
    )
    args = parser.parse_args(argv)
    project_root = args.project_root.resolve()
    requirements_lock = (
        args.requirements_lock
        if args.requirements_lock.is_absolute()
        else project_root / args.requirements_lock
    ).resolve()

    with tempfile.TemporaryDirectory(prefix="engineering-process-adoption-") as directory:
        environment_root = Path(directory).resolve()
        try:
            environment_root.relative_to(project_root)
        except ValueError:
            pass
        else:
            raise RuntimeError("temporary adoption environment must be outside checkout")
        _run([sys.executable, "-I", "-m", "venv", str(environment_root)], cwd=project_root)
        python = environment_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        _run(
            [
                str(python),
                "-I",
                "-m",
                "pip",
                "install",
                "--isolated",
                "--disable-pip-version-check",
                "--no-input",
                "--require-hashes",
                "--only-binary",
                ":all:",
                "-r",
                str(requirements_lock),
            ],
            cwd=environment_root,
        )
        output = _run(
            [
                str(python),
                "-I",
                "-m",
                "engineering_process",
                "adoption",
                "apply",
                "--project-root",
                str(project_root),
                "--requirements-lock",
                str(requirements_lock),
                "--json",
            ],
            cwd=environment_root,
        )
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as error:
        print(f"process adoption failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
