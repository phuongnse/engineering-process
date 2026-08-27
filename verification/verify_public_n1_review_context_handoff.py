from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import (
    CORE_QUALITY_DIMENSIONS,
    MATERIAL_DECISION_CATEGORIES,
    ContractError,
    canonical_json_digest,
)
from verification.qualify_release_lifecycle import _run
from verification.transfer_review_context_reservation import (
    export_reservation,
    restore_reservation,
)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _project_document() -> dict[str, Any]:
    passing = {
        "run": ["python", "-c", "raise SystemExit(0)"],
        "timeoutSeconds": 30,
    }
    return {
        "schemaVersion": 3,
        "project": "public-n1-handoff-fixture",
        "lifecycle": {
            "requiredProfiles": ["development", "review"],
            "planDecision": {
                "mode": "provenance-gated-authored-review",
                "materialCategories": list(MATERIAL_DECISION_CATEGORIES),
            },
        },
        "profiles": {
            "development": [{"id": "unit", **passing}],
            "review": [{"id": "review", **passing}],
        },
        "environment": {
            "defaultProfile": "development",
            "foregroundOnly": True,
            "managedTools": [],
            "profiles": {
                "development": ["python-runtime"],
                "review": ["python-runtime"],
            },
            "requirements": [
                {
                    "id": "python-runtime",
                    "description": "Exact public N-1 fixture runtime",
                    "probe": {
                        "run": ["python", "--version"],
                        "timeoutSeconds": 15,
                        "readOnly": True,
                        "outputStream": "combined",
                        "outputRegex": "^Python 3\\.(?:11|12|13|14)\\.",
                    },
                    "remediation": "Run with the supported public N-1 Python.",
                }
            ],
            "setupActions": [],
        },
    }


def _contract_document(comparison_base: str) -> dict[str, Any]:
    return {
        "schemaVersion": 3,
        "id": "public-n1-handoff-fixture",
        "summary": "Prove exact public N-1 plan-review continuation state",
        "source": "Mechanical verification fixture; grants no release authority",
        "comparisonBase": comparison_base,
        "specification": {
            "kind": "change-contract",
            "reference": "review context reservation handoff",
            "rationale": "The fixture proves lifecycle transport mechanics only.",
        },
        "risk": "high",
        "affectedProjects": ["public-n1-handoff-fixture"],
        "acceptanceCriteria": [
            {
                "id": "ac-handoff",
                "outcome": "The exact assigned context reservation survives the isolated handoff and authorizes implementation only after its bound review.",
            }
        ],
        "requiredProfiles": ["development", "review"],
        "quality": {
            "standard": "production-v1",
            "assessments": [
                {
                    "dimension": dimension,
                    "status": "applicable",
                    "rationale": "The mechanical fixture exercises the complete governed handoff.",
                    "criteria": ["ac-handoff"],
                }
                for dimension in CORE_QUALITY_DIMENSIONS
            ],
        },
        "signOff": {
            "required": False,
            "status": "not-required",
            "evidence": None,
        },
    }


def _plan_document(
    *, contract_digest: str, authority: dict[str, str]
) -> dict[str, Any]:
    return {
        "schemaVersion": 3,
        "changeId": "public-n1-handoff-fixture",
        "contractDigest": contract_digest,
        "approach": "Transport the exact assignment-bound reservation before implementation.",
        "workItems": [
            {
                "id": "work-handoff",
                "outcome": "Exercise the reviewed public N-1 continuation",
                "affectedPaths": ["tracked.txt"],
                "verificationProfiles": ["development", "review"],
            }
        ],
        "acceptancePlan": [
            {
                "criterionId": "ac-handoff",
                "workItems": ["work-handoff"],
                "verificationProfiles": ["development", "review"],
            }
        ],
        "risks": [],
        "openDecisions": [],
        "provenance": {
            "kind": "authored",
            "author": {
                "actorId": "fixture-author",
                "contextId": "fixture-author-context",
                "kind": "agent",
            },
            "authority": authority,
        },
    }


def _mechanical_review(assignment: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "kind": "engineering-process-plan-decision-review",
        "changeId": assignment["changeId"],
        "cycle": assignment["cycle"],
        "contractSha256": assignment["contractSha256"],
        "planSha256": assignment["planSha256"],
        "assignmentSha256": canonical_json_digest(assignment),
        "reviewer": assignment["reviewer"],
        "categoryAssessments": [
            {
                "category": category,
                "status": "clear",
                "evidence": "The isolated mechanical fixture exercises this assigned category without granting Release authority.",
            }
            for category in MATERIAL_DECISION_CATEGORIES
        ],
        "verdict": "clear",
    }


def verify_handoff(
    project_root: Path, processctl: Path, *, temporary_root: Path | None = None
) -> dict[str, Any]:
    project_root = project_root.resolve(strict=True)
    authority = Path(os.path.abspath(processctl.expanduser()))
    if (
        not authority.is_file()
        or authority.parent != Path(sys.executable).absolute().parent
    ):
        raise ContractError("handoff verification must run with the public N-1 Python")
    lock = json.loads(
        (project_root / ".process" / "process.lock").read_text(encoding="utf-8")
    )
    process_identity = lock["process"]
    temp_parent = (
        str(temporary_root.resolve(strict=True)) if temporary_root is not None else None
    )
    with tempfile.TemporaryDirectory(
        prefix="engineering-process-public-n1-handoff-", dir=temp_parent
    ) as directory:
        root = Path(directory)
        source = root / "source"
        source.mkdir()
        (source / ".process").mkdir()
        (source / ".gitignore").write_text(".process/runs/\n", encoding="utf-8")
        shutil.copyfile(
            project_root / ".process" / "process.lock",
            source / ".process" / "process.lock",
        )
        _write_json(source / ".process" / "project.json", _project_document())
        (source / "tracked.txt").write_text("fixture\n", encoding="utf-8")
        _run(["git", "init", "-q", "-b", "main"], cwd=source)
        _run(["git", "config", "user.email", "fixture@example.invalid"], cwd=source)
        _run(["git", "config", "user.name", "Handoff Fixture"], cwd=source)
        _run([str(authority), "sync"], cwd=source)
        _run(["git", "add", "."], cwd=source)
        _run(["git", "commit", "-qm", "test: seed handoff fixture"], cwd=source)
        checkpoint = _run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=source,
            capture=True,
        )
        inputs = root / "inputs"
        inputs.mkdir()
        contract_path = inputs / "contract.json"
        contract = _contract_document(checkpoint)
        _write_json(contract_path, contract)
        contract_digest = "sha256:" + hashlib.sha256(
            contract_path.read_bytes()
        ).hexdigest()
        plan_path = inputs / "plan.json"
        _write_json(
            plan_path,
            _plan_document(
                contract_digest=contract_digest, authority=process_identity
            ),
        )
        command = str(authority)
        for arguments in (
            (
                "change", "start", "--actor", "fixture-author", "--context",
                "fixture-author-context", "--actor-kind", "agent", "--contract",
                str(contract_path),
            ),
            (
                "change", "plan", "--actor", "fixture-author", "--context",
                "fixture-author-context", "--actor-kind", "agent", "--change-id",
                "public-n1-handoff-fixture", "--plan", str(plan_path),
            ),
            (
                "change", "decision", "start", "--actor", "fixture-reviewer",
                "--context", "fixture-reviewer-context", "--actor-kind", "agent",
                "--change-id", "public-n1-handoff-fixture", "--method",
                "isolated-context", "--attested-by", "mechanical-fixture",
                "--attestation-evidence",
                "Fresh isolated fixture context used only to prove public N-1 transport mechanics",
            ),
        ):
            _run([command, *arguments], cwd=source)
        change_id = "public-n1-handoff-fixture"
        source_run = source / ".process" / "runs" / change_id
        assignment_path = source_run / "plan-decision-assignment-1.json"
        handoff = root / "review-contexts"
        export_reservation(source, assignment_path, handoff)
        restored = root / "restored"
        _run(["git", "clone", "-q", str(source), str(restored)], cwd=root)
        restored_run = restored / ".process" / "runs" / change_id
        restored_run.parent.mkdir(parents=True)
        shutil.copytree(source_run, restored_run)
        restored_assignment = restored_run / "plan-decision-assignment-1.json"
        restore_reservation(restored, restored_assignment, handoff)
        assignment = json.loads(restored_assignment.read_text(encoding="utf-8"))
        review_path = inputs / "review.json"
        _write_json(review_path, _mechanical_review(assignment))
        implementation_context = f"fixture-implementation-{checkpoint}"
        for arguments in (
            (
                "change", "decision", "submit", "--change-id", change_id,
                "--review", str(review_path),
            ),
            (
                "change", "implement", "--actor", "fixture-implementer",
                "--context", implementation_context, "--actor-kind", "agent",
                "--change-id", change_id,
            ),
            (
                "change", "verify", "--actor", "fixture-implementer", "--context",
                implementation_context, "--actor-kind", "agent", "--change-id",
                change_id, "--profile", "development",
            ),
            (
                "change", "verify", "--actor", "fixture-implementer", "--context",
                implementation_context, "--actor-kind", "agent", "--change-id",
                change_id, "--profile", "review",
            ),
        ):
            _run([command, *arguments], cwd=restored)
        status_text = _run(
            [command, "change", "status", "--change-id", change_id, "--json"],
            cwd=restored,
            capture=True,
        )
        status = json.loads(status_text)
        if (
            status.get("status") != "passed"
            or status.get("phase") != "verified"
            or {item["profile"] for item in status.get("verification", [])}
            != {"development", "review"}
        ):
            raise ContractError("public N-1 handoff fixture did not reach verified")
    return {
        "status": "passed",
        "authority": process_identity,
        "phase": "verified",
        "profiles": ["development", "review"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--processctl", type=Path, required=True)
    parser.add_argument("--temporary-root", type=Path)
    arguments = parser.parse_args()
    try:
        result = verify_handoff(
            arguments.project_root,
            arguments.processctl,
            temporary_root=arguments.temporary_root,
        )
    except (ContractError, OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
