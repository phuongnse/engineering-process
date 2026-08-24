from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from typing import Protocol, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.bounded_process import run_bounded_process
from engineering_process.contracts import ContractError
from engineering_process.diagnostics import (
    classify_diagnostics,
    diagnostic_failure_message,
)
from verification.select_release_evidence import (
    MAX_EVIDENCE_BYTES,
    select_release_evidence,
)


COMMAND_TIMEOUT_SECONDS = 120
COMMAND_OUTPUT_STREAM_LIMIT = 1_000_000
COMMAND_OUTPUT_TOTAL_LIMIT = 1_500_000
REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SENSITIVE_ENVIRONMENT_MARKERS = (
    "CREDENTIAL",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
)


class GitHubClient(Protocol):
    def actions_artifacts(self, *, artifact_name: str) -> object: ...

    def published_release(self, *, tag: str) -> object: ...

    def verify_release(self, *, tag: str) -> None: ...

    def download_actions_artifact(
        self, *, run_id: int, artifact_name: str, output: Path
    ) -> None: ...

    def download_release_asset(
        self, *, tag: str, asset_name: str, output: Path
    ) -> None: ...


def _safe_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in SENSITIVE_ENVIRONMENT_MARKERS)
    }
    if "GH_TOKEN" in os.environ:
        environment["GH_TOKEN"] = os.environ["GH_TOKEN"]
    return environment


class CliGitHubClient:
    def __init__(self, *, repository: str, working_directory: Path) -> None:
        self.repository = repository
        self.working_directory = working_directory

    def _run(self, command: Sequence[str]) -> bytes:
        try:
            result = run_bounded_process(
                ("gh", *command),
                working_directory=self.working_directory,
                environment=_safe_environment(),
                timeout_seconds=COMMAND_TIMEOUT_SECONDS,
                max_stream_bytes=COMMAND_OUTPUT_STREAM_LIMIT,
                max_total_bytes=COMMAND_OUTPUT_TOTAL_LIMIT,
            )
        except (OSError, ValueError) as error:
            raise ContractError(f"cannot execute bounded GitHub command: {error}") from error
        if result.timed_out:
            raise ContractError(
                f"GitHub command exceeded {COMMAND_TIMEOUT_SECONDS} seconds"
            )
        if result.output_exceeded:
            raise ContractError("GitHub command output exceeded its bounded limit")
        if result.descendants_found or result.cleanup_error is not None:
            raise ContractError(
                result.cleanup_error or "GitHub command left descendant processes"
            )
        if result.returncode != 0:
            combined = result.stdout + result.stderr
            raise ContractError(
                "GitHub command failed: "
                f"exit={result.returncode}; bytes={len(combined)}; "
                f"sha256:{hashlib.sha256(combined).hexdigest()}"
            )
        diagnostics = classify_diagnostics(stdout=result.stdout, stderr=result.stderr)
        diagnostic_error = diagnostic_failure_message(
            diagnostics, subject="GitHub command"
        )
        if diagnostic_error is not None:
            raise ContractError(diagnostic_error)
        return result.stdout

    def _json(self, command: Sequence[str], *, label: str) -> object:
        content = self._run(command)
        try:
            return json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContractError(f"{label} is not valid UTF-8 JSON") from error

    def actions_artifacts(self, *, artifact_name: str) -> object:
        return self._json(
            (
                "api",
                "--method",
                "GET",
                f"repos/{self.repository}/actions/artifacts",
                "-f",
                f"name={artifact_name}",
                "-f",
                "per_page=100",
            ),
            label="Actions artifact metadata",
        )

    def published_release(self, *, tag: str) -> object:
        return self._json(
            (
                "release",
                "view",
                tag,
                "--repo",
                self.repository,
                "--json",
                "tagName,name,isDraft,publishedAt,assets",
            ),
            label="published release metadata",
        )

    def verify_release(self, *, tag: str) -> None:
        self._run(("release", "verify", tag, "--repo", self.repository))

    def download_actions_artifact(
        self, *, run_id: int, artifact_name: str, output: Path
    ) -> None:
        self._run(
            (
                "run",
                "download",
                str(run_id),
                "--repo",
                self.repository,
                "--name",
                artifact_name,
                "--dir",
                str(output),
            )
        )

    def download_release_asset(
        self, *, tag: str, asset_name: str, output: Path
    ) -> None:
        self._run(
            (
                "release",
                "download",
                tag,
                "--repo",
                self.repository,
                "--pattern",
                asset_name,
                "--dir",
                str(output),
            )
        )


def restore_release_evidence(
    *,
    client: GitHubClient,
    reviewed_sha: str,
    tag: str,
    evidence_asset: str,
    output: Path,
) -> dict[str, object]:
    if SHA_PATTERN.fullmatch(reviewed_sha) is None:
        raise ContractError("reviewed SHA must be a full lowercase Git SHA")
    artifact_name = f"release-completion-{reviewed_sha}"
    actions = client.actions_artifacts(artifact_name=artifact_name)
    selection = select_release_evidence(
        actions_document=actions,
        expected_actions_artifact=artifact_name,
        expected_tag=tag,
        evidence_asset=evidence_asset,
    )
    if output.exists() or output.is_symlink():
        raise ContractError("release evidence output directory must not already exist")
    output.mkdir(parents=True)
    try:
        if selection["source"] == "actions":
            run_id = selection["runId"]
            assert isinstance(run_id, int)
            client.download_actions_artifact(
                run_id=run_id,
                artifact_name=artifact_name,
                output=output,
            )
        else:
            if selection["source"] != "published-release-required":
                raise ContractError("release evidence selection returned an invalid source")
            release = client.published_release(tag=tag)
            client.verify_release(tag=tag)
            if not isinstance(release, dict):
                raise ContractError("published release metadata must be a JSON object")
            release = {**release, "immutabilityVerified": True}
            selection = select_release_evidence(
                actions_document=actions,
                expected_actions_artifact=artifact_name,
                expected_tag=tag,
                evidence_asset=evidence_asset,
                release_document=release,
            )
            if selection["source"] != "published-release":
                raise ContractError("published release evidence selection did not converge")
            client.download_release_asset(
                tag=tag,
                asset_name=evidence_asset,
                output=output,
            )
        evidence = output / evidence_asset
        try:
            state = evidence.lstat()
        except OSError as error:
            raise ContractError(f"cannot inspect restored release evidence: {error}") from error
        if (
            evidence.is_symlink()
            or not stat.S_ISREG(state.st_mode)
            or state.st_size < 1
            or state.st_size > MAX_EVIDENCE_BYTES
        ):
            raise ContractError(
                "restored release evidence must be one regular non-symlink file "
                f"between 1 and {MAX_EVIDENCE_BYTES} bytes"
            )
        if (
            selection["source"] == "published-release"
            and selection.get("size") != state.st_size
        ):
            raise ContractError(
                "restored release evidence size differs from selected metadata"
            )
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return selection


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--reviewed-sha", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--evidence-asset", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        if REPOSITORY_PATTERN.fullmatch(arguments.repository) is None:
            raise ContractError("repository must be one bounded owner/name identity")
        selection = restore_release_evidence(
            client=CliGitHubClient(
                repository=arguments.repository,
                working_directory=Path.cwd(),
            ),
            reviewed_sha=arguments.reviewed_sha,
            tag=arguments.tag,
            evidence_asset=arguments.evidence_asset,
            output=arguments.output,
        )
    except (ContractError, OSError) as error:
        print(f"release evidence recovery failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(selection, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
