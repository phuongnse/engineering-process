from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import stat
from typing import Any


MAX_PROVIDER_DOCUMENT_BYTES = 1_000_000
MAX_PROVIDER_PAGES = 100
MAX_PROVIDER_ARTIFACTS = 1_000
RELEASE_CANDIDATE_WORKFLOW_PATH = ".github/workflows/release-candidate.yml"


class ContinuationError(ValueError):
    pass


def _read_json(path: Path, label: str) -> Any:
    try:
        file_stat = path.lstat()
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise ContinuationError(f"{label} must be a regular non-symlink file")
        if file_stat.st_size > MAX_PROVIDER_DOCUMENT_BYTES:
            raise ContinuationError(f"{label} exceeds its byte limit")
        content = path.read_bytes()
    except OSError as error:
        raise ContinuationError(f"cannot read {label}: {error}") from error
    if len(content) != file_stat.st_size:
        raise ContinuationError(f"{label} changed while reading")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContinuationError(f"{label} is not valid UTF-8 JSON") from error
    return value


def _read_object(path: Path, label: str) -> dict[str, Any]:
    value = _read_json(path, label)
    if not isinstance(value, dict):
        raise ContinuationError(f"{label} must be a JSON object")
    return value


def _read_array(path: Path, label: str) -> list[Any]:
    value = _read_json(path, label)
    if not isinstance(value, list):
        raise ContinuationError(f"{label} must be a JSON array")
    return value


def _provider_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContinuationError(f"{label} must be a positive integer")
    return value


def _repository(value: Any, label: str) -> str:
    if not isinstance(value, dict) or value.get("full_name") != label:
        raise ContinuationError("planned run repository identity does not match")
    return label


def select_planned_artifact(
    pages: list[Any], *, expected_name: str
) -> dict[str, Any]:
    if (
        re.fullmatch(r"planned-release-candidate-[0-9a-f]{40}", expected_name)
        is None
    ):
        raise ContinuationError("planned artifact name is invalid")
    if not pages or len(pages) > MAX_PROVIDER_PAGES:
        raise ContinuationError("planned artifact pages are missing or exceed their limit")
    artifacts: list[dict[str, Any]] = []
    for index, raw_page in enumerate(pages):
        if not isinstance(raw_page, dict) or not isinstance(
            raw_page.get("artifacts"), list
        ):
            raise ContinuationError(
                f"planned artifact page {index} has an invalid contract"
            )
        for artifact in raw_page["artifacts"]:
            if not isinstance(artifact, dict):
                raise ContinuationError("planned artifact entry must be an object")
            artifacts.append(artifact)
            if len(artifacts) > MAX_PROVIDER_ARTIFACTS:
                raise ContinuationError("planned artifacts exceed their count limit")
    candidates = [
        artifact
        for artifact in artifacts
        if artifact.get("name") == expected_name and artifact.get("expired") is False
    ]
    if len(candidates) != 1:
        raise ContinuationError(
            "planned run must contain exactly one unexpired expected artifact"
        )
    artifact_id = _provider_integer(candidates[0].get("id"), "planned artifact id")
    return {"id": artifact_id, "name": expected_name}


def require_artifact_absent(pages: list[Any], *, artifact_id: int) -> None:
    _provider_integer(artifact_id, "planned artifact id")
    remaining: list[int] = []
    for raw_page in pages:
        if not isinstance(raw_page, dict) or not isinstance(
            raw_page.get("artifacts"), list
        ):
            raise ContinuationError("remaining artifact pages have an invalid contract")
        for artifact in raw_page["artifacts"]:
            if isinstance(artifact, dict) and artifact.get("id") == artifact_id:
                remaining.append(artifact_id)
    if remaining:
        raise ContinuationError("planned artifact still exists after terminal cleanup")


def validate_continuation(
    run: dict[str, Any],
    workflow: dict[str, Any],
    *,
    repository: str,
    planned_run_id: int,
    planned_run_attempt: int,
    protected_base: str,
) -> dict[str, Any]:
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise ContinuationError("repository identity is invalid")
    if re.fullmatch(r"[0-9a-f]{40}", protected_base) is None:
        raise ContinuationError("protected base must be a full lowercase Git SHA")
    if _provider_integer(run.get("id"), "planned run id") != planned_run_id:
        raise ContinuationError("planned run id does not match")
    if (
        _provider_integer(run.get("run_attempt"), "planned run attempt")
        != planned_run_attempt
    ):
        raise ContinuationError("planned run attempt does not match")
    if run.get("event") != "workflow_dispatch":
        raise ContinuationError("planned run event must be workflow_dispatch")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise ContinuationError("planned run must be terminal and successful")
    if run.get("path") != RELEASE_CANDIDATE_WORKFLOW_PATH:
        raise ContinuationError("planned run workflow path does not match")
    if workflow.get("path") != RELEASE_CANDIDATE_WORKFLOW_PATH:
        raise ContinuationError("protected workflow path does not match")
    workflow_id = _provider_integer(workflow.get("id"), "protected workflow id")
    if _provider_integer(run.get("workflow_id"), "planned run workflow id") != workflow_id:
        raise ContinuationError("planned run workflow id does not match")
    if run.get("head_sha") != protected_base:
        raise ContinuationError("planned run did not execute at the protected base")
    _repository(run.get("repository"), repository)
    _repository(run.get("head_repository"), repository)
    return {
        "schemaVersion": 1,
        "kind": "engineering-process-release-plan-continuation-source",
        "repository": repository,
        "workflow": {
            "id": workflow_id,
            "path": RELEASE_CANDIDATE_WORKFLOW_PATH,
            "sha": protected_base,
        },
        "run": {
            "id": planned_run_id,
            "attempt": planned_run_attempt,
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
        },
        "controls": {
            "grantsImplementation": False,
            "grantsReview": False,
            "grantsCompletion": False,
            "grantsPublication": False,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--workflow", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--artifact-name", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--planned-run-id", type=int, required=True)
    parser.add_argument("--planned-run-attempt", type=int, required=True)
    parser.add_argument("--protected-base", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        result = validate_continuation(
            _read_object(arguments.run, "planned run"),
            _read_object(arguments.workflow, "protected workflow"),
            repository=arguments.repository,
            planned_run_id=arguments.planned_run_id,
            planned_run_attempt=arguments.planned_run_attempt,
            protected_base=arguments.protected_base,
        )
        result["artifact"] = select_planned_artifact(
            _read_array(arguments.artifacts, "planned artifact pages"),
            expected_name=arguments.artifact_name,
        )
        arguments.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (ContinuationError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
