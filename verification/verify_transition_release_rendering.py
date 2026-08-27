from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.bounded_process import run_bounded_process
from engineering_process.contracts import ContractError, read_json, validate_release


OUTPUT_LIMIT = 128_000


def _run(command: list[str], *, cwd: Path) -> tuple[int, bytes, bytes]:
    result = run_bounded_process(
        command,
        working_directory=cwd,
        environment={
            key: value
            for key, value in os.environ.items()
            if key in {"COMSPEC", "PATHEXT", "SystemRoot", "WINDIR"}
        }
        | {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        timeout_seconds=60,
        max_stream_bytes=OUTPUT_LIMIT,
        max_total_bytes=OUTPUT_LIMIT,
    )
    if (
        result.timed_out
        or result.output_exceeded
        or result.descendants_found
        or result.cleanup_error is not None
        or result.input_error
    ):
        raise ContractError(
            result.cleanup_error or "transition release rendering lost its process boundary"
        )
    return result.returncode or 0, result.stdout, result.stderr


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prove schema-4 rendering uses fixed source under a real public 0.7 runtime"
    )
    parser.add_argument("--public-python", type=Path, required=True)
    parser.add_argument("--controller", type=Path, required=True)
    parser.add_argument(
        "--controller-requirement",
        action="append",
        type=Path,
    )
    parser.add_argument("--release", type=Path, required=True)
    args = parser.parse_args(argv)
    public_python = Path(os.path.abspath(os.fspath(args.public_python)))
    if not public_python.is_file():
        raise ContractError("public authority interpreter is unavailable")
    controller = args.controller.resolve(strict=True)
    release_path = args.release.resolve(strict=True)
    validate_release(read_json(release_path), str(release_path))
    controller_root = controller.parent
    expected_requirements = [
        controller_root / "engineering_process" / "requirements-runtime.txt",
        controller_root / "engineering_process" / "requirements-dev.txt",
        controller_root / "engineering_process" / "requirements-build.txt",
    ]
    requirements = (
        [path.resolve(strict=True) for path in args.controller_requirement]
        if args.controller_requirement
        else expected_requirements
    )
    if requirements != expected_requirements:
        raise ContractError(
            "transition release rendering requires exact runtime, development, and build controller requirements"
        )
    install_command = [
        str(public_python),
        "-I",
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
    ]
    for requirement in requirements:
        install_command.extend(("-r", str(requirement)))
    install_code, _install_stdout, install_stderr = _run(
        install_command,
        cwd=controller_root,
    )
    if install_code != 0 or install_stderr:
        raise ContractError(
            "public authority proof could not install exact controller dependencies"
        )

    version_code, version_stdout, version_stderr = _run(
        [
            str(public_python),
            "-I",
            "-c",
            "import engineering_process; print(engineering_process.VERSION)",
        ],
        cwd=PROJECT_ROOT,
    )
    if version_code != 0 or version_stderr or version_stdout.strip() != b"0.7.0":
        raise ContractError("release rendering proof requires exact public 0.7.0")

    with tempfile.TemporaryDirectory(
        prefix="engineering-process-transition-rendering-"
    ) as directory:
        root = Path(directory)
        shutil.copy2(release_path, root / "release.json")
        old_output = root / "old-reader.md"
        old_code, old_stdout, old_stderr = _run(
            [
                str(public_python),
                "-I",
                "-m",
                "engineering_process",
                "publication",
                "release-pr-body",
                "--project-root",
                str(root),
                "--state",
                "approved",
                "--output",
                str(old_output),
                "--json",
            ],
            cwd=root,
        )
        if old_code == 0 or old_output.exists() or not (old_stdout or old_stderr):
            raise ContractError("public 0.7.0 unexpectedly interpreted release schema 4")

        source_output = root / "source-reader.md"
        source_code, source_stdout, source_stderr = _run(
            [
                str(public_python),
                str(controller),
                "publication",
                "release-pr-body",
                "--project-root",
                str(root),
                "--state",
                "approved",
                "--output",
                str(source_output),
                "--json",
            ],
            cwd=root,
        )
        if source_code != 0 or source_stderr:
            raise ContractError("fixed source reader failed under public 0.7.0 runtime")
        try:
            result = json.loads(source_stdout.decode("utf-8"))
            body = source_output.read_text(encoding="utf-8")
        except (UnicodeDecodeError, json.JSONDecodeError, OSError) as error:
            raise ContractError("fixed source renderer returned invalid evidence") from error
        if (
            result.get("status") != "passed"
            or "<!-- engineering-process:pr-description:start -->" not in body
            or "`authority-transition-bootstrap`" not in body
        ):
            raise ContractError("fixed source renderer omitted transition release semantics")
    print(
        json.dumps(
            {
                "publicAuthority": "0.7.0",
                "controllerRequirementCount": len(requirements),
                "releaseSchema": 4,
                "sourceReader": str(controller),
                "status": "passed",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ContractError, OSError) as error:
        print(f"transition release rendering failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
