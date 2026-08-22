from __future__ import annotations

import argparse
import json
from pathlib import Path
import stat
import sys
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from engineering_process.artifact_attestation import MAX_ATTESTATION_BYTES
from engineering_process.contracts import ContractError, read_json, validate_release


MAX_PYPI_RESPONSE_BYTES = 2_000_000
REQUEST_TIMEOUT_SECONDS = 15
MAX_WAIT_SECONDS = 300
POLL_INTERVAL_SECONDS = 5


def _bounded_bytes(path: Path, *, limit: int, label: str) -> bytes:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ContractError(f"{label} must be a regular non-symlink file")
        if before.st_size > limit:
            raise ContractError(f"{label} exceeds {limit} bytes")
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
        after = path.lstat()
    except OSError as error:
        raise ContractError(f"cannot read {label}: {error}") from error
    if len(content) > limit:
        raise ContractError(f"{label} exceeds {limit} bytes")
    if (
        len(content) != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_mode != before.st_mode
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
    ):
        raise ContractError(f"{label} changed while reading")
    return content


def _expected_files(
    project_root: Path, attestation_path: Path
) -> tuple[str, str, dict[str, tuple[int, str]]]:
    release = validate_release(
        read_json(project_root / "release.json"),
        str(project_root / "release.json"),
    )
    if release.package_name is None:
        raise ContractError("PyPI inspection requires a package release identity")
    try:
        document = json.loads(
            _bounded_bytes(
                attestation_path,
                limit=MAX_ATTESTATION_BYTES,
                label="artifact attestation",
            ).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"invalid artifact attestation: {error}") from error
    identity = document.get("release") if isinstance(document, dict) else None
    artifacts = document.get("artifacts") if isinstance(document, dict) else None
    if (
        not isinstance(identity, dict)
        or identity.get("package") != release.package_name
        or identity.get("version") != release.version
        or identity.get("artifacts") != list(release.artifacts)
        or not isinstance(artifacts, list)
    ):
        raise ContractError("artifact attestation does not match release identity")
    expected: dict[str, tuple[int, str]] = {}
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "sha256",
            "sizeBytes",
        }:
            raise ContractError("artifact attestation has an invalid file identity")
        name = item["name"]
        size = item["sizeBytes"]
        digest = item["sha256"]
        if (
            name not in release.artifacts
            or name in expected
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or len(digest) != 71
            or any(
                character not in "0123456789abcdef"
                for character in digest.removeprefix("sha256:")
            )
        ):
            raise ContractError("artifact attestation has an invalid file identity")
        expected[name] = (size, digest.removeprefix("sha256:"))
    if sorted(expected) != sorted(release.artifacts):
        raise ContractError("artifact attestation file set does not match release")
    return release.package_name, release.version, expected


def _pypi_document(
    package: str,
    version: str,
    *,
    opener: Callable[..., object],
) -> dict[str, object] | None:
    url = (
        "https://pypi.org/pypi/"
        f"{quote(package, safe='')}/{quote(version, safe='')}/json"
    )
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "engineering-process-release-verifier/1",
        },
    )
    try:
        response = opener(request, timeout=REQUEST_TIMEOUT_SECONDS)
    except HTTPError as error:
        if error.code == 404:
            error.close()
            return None
        raise ContractError(f"PyPI returned HTTP {error.code}") from error
    except (OSError, URLError) as error:
        raise ContractError(f"cannot query PyPI: {error}") from error
    try:
        with response:
            status = getattr(response, "status", None)
            if status != 200:
                raise ContractError(f"PyPI returned HTTP {status}")
            length_header = response.headers.get("Content-Length")
            if length_header is not None:
                try:
                    declared_length = int(length_header)
                except ValueError as error:
                    raise ContractError(
                        "PyPI returned an invalid Content-Length"
                    ) from error
                if declared_length > MAX_PYPI_RESPONSE_BYTES:
                    raise ContractError("PyPI response exceeds the size limit")
            content = response.read(MAX_PYPI_RESPONSE_BYTES + 1)
    except OSError as error:
        raise ContractError(f"cannot read PyPI response: {error}") from error
    if len(content) > MAX_PYPI_RESPONSE_BYTES:
        raise ContractError("PyPI response exceeds the size limit")
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"PyPI returned invalid JSON: {error}") from error
    if not isinstance(document, dict):
        raise ContractError("PyPI response must be a JSON object")
    return document


def _simple_document(
    package: str,
    *,
    opener: Callable[..., object],
) -> dict[str, object]:
    url = f"https://pypi.org/simple/{quote(package, safe='')}/"
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.pypi.simple.v1+json",
            "User-Agent": "engineering-process-release-verifier/1",
        },
    )
    try:
        response = opener(request, timeout=REQUEST_TIMEOUT_SECONDS)
    except HTTPError as error:
        raise ContractError(f"PyPI Simple API returned HTTP {error.code}") from error
    except (OSError, URLError) as error:
        raise ContractError(f"cannot query PyPI Simple API: {error}") from error
    try:
        with response:
            status = getattr(response, "status", None)
            if status != 200:
                raise ContractError(f"PyPI Simple API returned HTTP {status}")
            length_header = response.headers.get("Content-Length")
            if length_header is not None:
                try:
                    declared_length = int(length_header)
                except ValueError as error:
                    raise ContractError(
                        "PyPI Simple API returned an invalid Content-Length"
                    ) from error
                if declared_length > MAX_PYPI_RESPONSE_BYTES:
                    raise ContractError("PyPI Simple API response exceeds the size limit")
            content = response.read(MAX_PYPI_RESPONSE_BYTES + 1)
    except OSError as error:
        raise ContractError(f"cannot read PyPI Simple API response: {error}") from error
    if len(content) > MAX_PYPI_RESPONSE_BYTES:
        raise ContractError("PyPI Simple API response exceeds the size limit")
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"PyPI Simple API returned invalid JSON: {error}") from error
    if not isinstance(document, dict):
        raise ContractError("PyPI Simple API response must be a JSON object")
    return document


def inspect_pypi_publication(
    package: str,
    version: str,
    expected: dict[str, tuple[int, str]],
    *,
    opener: Callable[..., object] = urlopen,
) -> dict[str, object]:
    document = _pypi_document(package, version, opener=opener)
    if document is None:
        return {
            "files": [],
            "package": package,
            "publishRequired": True,
            "status": "missing",
            "version": version,
        }
    info = document.get("info")
    urls = document.get("urls")
    if (
        not isinstance(info, dict)
        or info.get("name", "").casefold() != package.casefold()
        or info.get("version") != version
        or not isinstance(urls, list)
    ):
        raise ContractError("PyPI response does not match the release identity")
    actual: dict[str, tuple[int, str]] = {}
    for item in urls:
        if not isinstance(item, dict):
            raise ContractError("PyPI response contains an invalid file record")
        name = item.get("filename")
        size = item.get("size")
        digests = item.get("digests")
        digest = digests.get("sha256") if isinstance(digests, dict) else None
        if (
            not isinstance(name, str)
            or name in actual
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not isinstance(digest, str)
        ):
            raise ContractError("PyPI response contains an invalid file record")
        actual[name] = (size, digest)
    if actual != expected:
        raise ContractError(
            "PyPI version conflicts with the immutable release artifacts: "
            f"expected {sorted(expected)}, got {sorted(actual)}"
        )
    simple = _simple_document(package, opener=opener)
    simple_files = simple.get("files")
    if (
        simple.get("name", "").casefold() != package.casefold()
        or not isinstance(simple_files, list)
    ):
        raise ContractError("PyPI Simple API response does not match the package")
    visible: dict[str, str] = {}
    for item in simple_files:
        if not isinstance(item, dict):
            raise ContractError("PyPI Simple API contains an invalid file record")
        name = item.get("filename")
        if name not in expected:
            continue
        hashes = item.get("hashes")
        digest = hashes.get("sha256") if isinstance(hashes, dict) else None
        if name in visible or not isinstance(digest, str):
            raise ContractError("PyPI Simple API contains an invalid file record")
        visible[name] = digest
    if set(visible) != set(expected):
        return {
            "files": [],
            "package": package,
            "publishRequired": False,
            "status": "propagating",
            "version": version,
        }
    if any(visible[name] != expected[name][1] for name in expected):
        raise ContractError(
            "PyPI Simple API conflicts with the immutable release artifacts"
        )
    return {
        "files": [
            {"name": name, "sha256": digest, "sizeBytes": size}
            for name, (size, digest) in sorted(actual.items())
        ],
        "package": package,
        "publishRequired": False,
        "status": "published",
        "version": version,
    }


def wait_for_pypi_publication(
    package: str,
    version: str,
    expected: dict[str, tuple[int, str]],
    *,
    wait_seconds: int,
    opener: Callable[..., object] = urlopen,
) -> dict[str, object]:
    if wait_seconds < 0 or wait_seconds > MAX_WAIT_SECONDS:
        raise ContractError(
            f"wait-seconds must be between 0 and {MAX_WAIT_SECONDS}"
        )
    deadline = time.monotonic() + wait_seconds
    while True:
        result = inspect_pypi_publication(
            package,
            version,
            expected,
            opener=opener,
        )
        if result["status"] == "published":
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ContractError(
                f"PyPI {package} {version} did not become visible within "
                f"{wait_seconds} seconds"
            )
        time.sleep(min(POLL_INTERVAL_SECONDS, remaining))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--attestation", type=Path, required=True)
    parser.add_argument("--require-published", action="store_true")
    parser.add_argument("--wait-seconds", type=int, default=0)
    arguments = parser.parse_args()
    try:
        project_root = arguments.project_root.resolve(strict=True)
        package, version, expected = _expected_files(
            project_root,
            arguments.attestation,
        )
        if arguments.require_published:
            result = wait_for_pypi_publication(
                package,
                version,
                expected,
                wait_seconds=arguments.wait_seconds,
            )
        else:
            if arguments.wait_seconds != 0:
                raise ContractError(
                    "--wait-seconds requires --require-published"
                )
            result = inspect_pypi_publication(package, version, expected)
    except Exception as error:
        print(f"PyPI publication check: FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps(result, indent=2, sort_keys=True))
