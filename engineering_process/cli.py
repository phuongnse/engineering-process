from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from . import VERSION
from .adoption import apply_adoption, check_adoption
from .artifact_attestation import validate_distribution_attestation
from .bundles import load_bundles, select_bundles
from .bootstrap import initialize_project
from .contracts import (
    ContractError,
    canonical_json_digest,
    derive_release_version,
    read_json,
    validate_adoption_migration,
    validate_automation_policy,
    validate_automation_proposal,
    validate_automation_proposal_policy,
    validate_change,
    validate_improvement_catalog,
    validate_improvement_disposition,
    validate_improvement_reproduction,
    validate_improvement_resolution,
    validate_improvement_signal,
    validate_plan,
    validate_plan_decision_review,
    validate_plan_decision_review_assignment,
    validate_process_lock,
    validate_project,
    validate_recommendation,
    validate_recommendation_resolution,
    validate_recommendation_review,
    validate_recommendation_review_assignment,
    validate_remote_verification_evidence,
    validate_remote_verification_request,
    validate_release,
    validate_release_change,
    validate_review,
)
from .distribution import distribution_digest
from .environment import (
    doctor_environment,
    environment_command_bindings,
    environment_path_entries,
    execute_command,
    require_environment_profile,
    setup_environment,
)
from .evidence import (
    export_bootstrap_authorization,
    export_receipt,
    prune_completed_run,
    validate_bootstrap_authorization,
    validate_receipt,
)
from .evidence_transport import (
    COMPLETION_EVIDENCE_KINDS,
    encode_completion_evidence,
)
from .impact import plan_profile
from .improvement import (
    attach_improvement_chain,
    create_improvement_disposition,
    create_improvement_reproduction,
    create_improvement_resolution,
    export_improvement_signal,
    ingest_improvement_signal,
    observe_improvement_signal,
    validate_improvement_chain,
)
from .lifecycle import (
    begin_implementation,
    classify_improvement_case,
    finish_change,
    lifecycle_environment_issues,
    lifecycle_status,
    ingest_remote_verification,
    register_plan,
    resolve_plan_decision,
    request_remote_verification,
    start_change,
    start_plan_decision_review,
    start_review,
    submit_review,
    submit_plan_decision_review,
    verify_change,
)
from .runner import run_profile, source_state
from .publication import (
    MAX_PULL_REQUEST_BODY_BYTES,
    validate_controlled_automation_proposal,
    validate_controlled_automation_proposal_completion,
    validate_branch,
    validate_commit_range,
    validate_commit_subject,
    validate_completed_publication,
    validate_pull_request,
    validate_evidence_publication,
)
from .process_graph import load_process_graph, process_root_from_skills
from .recommendation import (
    create_recommendation_resolution,
    start_recommendation_review,
    validate_recommendation_chain,
)
from .release import validate_release_checkpoint
from .release_candidate import prepare_release_candidate, render_release_pull_request
from .skills import validate_skills
from .syncing import (
    default_process_root,
    load_lock,
    process_skills_root,
    sync_skills,
    synchronized_state,
)


def _root(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"directory does not exist: {value}")
    return path


def _write_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _result(name: str, *, status: str = "passed", **details: Any) -> dict[str, Any]:
    return {"command": name, "status": status, **details}


def _emit(args: argparse.Namespace, value: dict[str, Any]) -> None:
    if args.json:
        _write_json(value)
        return
    status = value["status"].upper()
    print(f"{value['command']}: {status}")
    for key, item in value.items():
        if key in {"command", "status"}:
            continue
        if isinstance(item, list):
            for entry in item:
                print(f"  {key}: {entry}")
        else:
            print(f"  {key}: {item}")


def command_project_validate(args: argparse.Namespace) -> int:
    path = args.project_root / ".process" / "project.json"
    project = validate_project(read_json(path), str(path))
    _emit(
        args,
        _result(
            "project validate",
            project=project.identifier,
            profiles=sorted(project.profiles),
            requiredProfiles=list(project.required_profiles),
            qualityExtensions=list(project.quality_extensions),
            environmentProfiles=(
                sorted(project.environment.profiles) if project.environment else []
            ),
            managedTools=(
                sorted(project.environment.managed_tools)
                if project.environment
                else []
            ),
        ),
    )
    return 0


def command_project_init(args: argparse.Namespace) -> int:
    result = initialize_project(
        args.project_root,
        args.process_root,
        manifest_path=args.manifest,
        requested_bundles=args.bundle,
        replace=args.replace,
    )
    _emit(args, _result("project init", **result))
    return 0


def command_lock_validate(args: argparse.Namespace) -> int:
    path = args.project_root / ".process" / "process.lock"
    lock = validate_process_lock(read_json(path), str(path))
    bundles = load_bundles(args.process_root, process_skills_root(args.process_root))
    missing_core = sorted(set(bundles["core"]) - set(lock.skills))
    if missing_core:
        raise ContractError(
            f"{path}: omits mandatory core skills: {', '.join(missing_core)}"
        )
    if lock.version != VERSION:
        raise ContractError(
            f"{path}: pins {lock.version}, but processctl is {VERSION}"
        )
    actual_digest = distribution_digest(args.process_root, lock.skills)
    if actual_digest != lock.digest:
        raise ContractError(
            f"{path}: digest {lock.digest} does not match source {actual_digest}"
        )
    _emit(
        args,
        _result(
            "lock validate",
            version=lock.version,
            digest=lock.digest,
            skills=list(lock.skills),
        ),
    )
    return 0


def command_lock_create(args: argparse.Namespace) -> int:
    lock_path = args.project_root / ".process" / "process.lock"
    if lock_path.exists() and not args.replace:
        raise ContractError(
            f"{lock_path}: already exists; use --replace for an intentional update"
        )
    skills_root = process_skills_root(args.process_root)
    bundles = load_bundles(args.process_root, skills_root)
    requested = select_bundles(bundles, args.bundle)
    skills = tuple(
        sorted(
            {
                skill
                for bundle in requested
                for skill in bundles[bundle]
            }
        )
    )
    document = {
        "schemaVersion": 1,
        "process": {
            "version": VERSION,
            "digest": distribution_digest(args.process_root, skills),
        },
        "skills": list(skills),
    }
    validate_process_lock(document, str(lock_path))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _emit(
        args,
        _result(
            "lock create",
            path=str(lock_path),
            version=VERSION,
            digest=document["process"]["digest"],
            skills=list(skills),
        ),
    )
    return 0


def command_adoption_apply(args: argparse.Namespace) -> int:
    details = apply_adoption(
        args.project_root,
        args.process_root,
        args.requirements_lock,
        requirements_source=args.requirements_source,
        expected_requirements_digest=args.expected_requirements_digest,
    )
    _emit(args, _result("adoption apply", **details))
    return 0


def command_adoption_check(args: argparse.Namespace) -> int:
    details = check_adoption(
        args.project_root,
        args.process_root,
        args.requirements_lock,
    )
    issues = details.pop("issues")
    status = "failed" if issues else "passed"
    _emit(args, _result("adoption check", status=status, issues=issues, **details))
    return 1 if issues else 0


def command_contract_validate(args: argparse.Namespace) -> int:
    document = read_json(args.path)
    validators: dict[str, Callable[[Any, str], Any]] = {
        "adoption-migration": validate_adoption_migration,
        "automation-policy": validate_automation_policy,
        "automation-proposal": validate_automation_proposal,
        "automation-proposal-policy": validate_automation_proposal_policy,
        "change": validate_change,
        "improvement-catalog": validate_improvement_catalog,
        "improvement-disposition": validate_improvement_disposition,
        "improvement-reproduction": validate_improvement_reproduction,
        "improvement-resolution": validate_improvement_resolution,
        "improvement-signal": validate_improvement_signal,
        "plan": validate_plan,
        "plan-decision-review": validate_plan_decision_review,
        "plan-decision-review-assignment": validate_plan_decision_review_assignment,
        "recommendation": validate_recommendation,
        "recommendation-resolution": validate_recommendation_resolution,
        "recommendation-review": validate_recommendation_review,
        "recommendation-review-assignment": validate_recommendation_review_assignment,
        "remote-verification-evidence": validate_remote_verification_evidence,
        "remote-verification-request": validate_remote_verification_request,
        "release": validate_release,
        "release-change": validate_release_change,
        "review": validate_review,
    }
    validators[args.kind](document, str(args.path))
    _emit(
        args,
        _result(
            "contract validate",
            kind=args.kind,
            path=str(args.path),
        ),
    )
    return 0


def command_recommendation_validate_chain(args: argparse.Namespace) -> int:
    _lifecycle_project(args)
    result = validate_recommendation_chain(
        args.project_root,
        args.recommendation,
        args.assignment,
        args.review,
        args.resolution,
    )
    status = "passed" if result["allowed"] else "blocked"
    _emit(
        args,
        _result("recommendation validate-chain", status=status, **result),
    )
    return 0 if result["allowed"] else 1


def command_recommendation_review_start(args: argparse.Namespace) -> int:
    _lifecycle_project(args)
    result = start_recommendation_review(
        args.project_root,
        args.recommendation,
        actor_id=args.actor,
        context_id=args.context,
        kind=args.actor_kind,
        method=args.method,
        attested_by=args.attested_by,
        evidence=args.attestation_evidence,
    )
    _emit(args, _result("recommendation review start", **result))
    return 0


def command_recommendation_resolution(args: argparse.Namespace) -> int:
    _lifecycle_project(args)
    result = create_recommendation_resolution(
        args.project_root,
        args.recommendation,
        args.assignment,
        args.review,
        selected_option_id=args.selected_option,
        owner_id=args.owner_id,
        owner_evidence_sha256=args.owner_evidence_sha256,
        selection_rationale_sha256=args.selection_rationale_sha256,
        output=args.output,
    )
    _emit(args, _result("recommendation resolution", **result))
    return 0


def command_improvement_validate_chain(args: argparse.Namespace) -> int:
    result = validate_improvement_chain(
        args.signal,
        args.disposition,
        args.resolution,
        args.reproduction,
        args.catalog,
    )
    _emit(args, _result("improvement validate-chain", **result))
    return 0


def command_improvement_status(args: argparse.Namespace) -> int:
    result = validate_improvement_chain(
        args.signal,
        args.disposition,
        args.resolution,
        args.reproduction,
        args.catalog,
    )
    _emit(args, _result("improvement status", **result))
    return 0


def command_improvement_classify(args: argparse.Namespace) -> int:
    _lifecycle_project(args)
    state = classify_improvement_case(
        args.project_root,
        args.change_id,
        args.case_id,
        owner_boundary=args.owner_boundary,
        reusable_class=args.reusable_class,
        invariant_id=args.invariant_id,
        disposition=args.disposition,
        rationale_sha256=args.rationale_sha256,
        target_project=args.target_project,
        target_repository=args.target_repository,
        actor_id=args.actor,
        context_id=args.context,
        kind=args.actor_kind,
    )
    case = next(
        item for item in state["improvements"] if item["id"] == args.case_id
    )
    _emit(
        args,
        _change_result(
            "improvement classify",
            state,
            caseId=case["id"],
            improvementPhase=case["phase"],
            invariantId=case["classification"]["invariantId"],
        ),
    )
    return 0


def command_improvement_export_signal(args: argparse.Namespace) -> int:
    _lifecycle_project(args)
    result = export_improvement_signal(
        args.project_root,
        args.change_id,
        args.case_id,
        source_repository=args.source_repository,
        affected_surfaces=args.affected_surface,
        reference=args.reference,
        output=args.output,
        actor_id=args.actor,
        context_id=args.context,
        actor_kind=args.actor_kind,
    )
    _emit(args, _result("improvement export-signal", **result))
    return 0


def command_improvement_observe(args: argparse.Namespace) -> int:
    _lifecycle_project(args)
    result = observe_improvement_signal(
        args.project_root,
        signal_id=args.signal_id,
        source_repository=args.source_repository,
        target_project=args.target_project,
        target_repository=args.target_repository,
        trigger_kind=args.trigger_kind,
        trigger_status=args.trigger_status,
        owner_boundary=args.owner_boundary,
        reusable_class=args.reusable_class,
        invariant_id=args.invariant_id,
        rationale_sha256=args.rationale_sha256,
        affected_surfaces=args.affected_surface,
        evidence_kind=args.evidence_kind,
        evidence_path=args.evidence,
        reference=args.reference,
        change_id=args.change_id,
        cycle=args.cycle,
        output=args.output,
    )
    _emit(args, _result("improvement observe", **result))
    return 0


def command_improvement_disposition(args: argparse.Namespace) -> int:
    _lifecycle_project(args)
    result = create_improvement_disposition(
        args.project_root,
        args.signal,
        args.catalog,
        producer_repository=args.producer_repository,
        decision=args.decision,
        owner_boundary=args.owner_boundary,
        reusable_class=args.reusable_class,
        invariant_id=args.invariant_id,
        linked_change_id=args.linked_change_id,
        rationale_sha256=args.rationale_sha256,
        exception_approved_by=args.exception_approved_by,
        exception_evidence_sha256=args.exception_evidence_sha256,
        output=args.output,
    )
    _emit(args, _result("improvement disposition", **result))
    return 0


def command_improvement_resolution(args: argparse.Namespace) -> int:
    result = create_improvement_resolution(
        args.project_root,
        args.signal,
        args.disposition,
        args.catalog,
        args.lifecycle_receipt,
        args.release_contract,
        args.release_receipt,
        args.release_authorization,
        args.artifact_root,
        args.artifact_attestation,
        release_repository=args.release_repository,
        release_tag=args.release_tag,
        release_name=args.release_name,
        release_commit=args.release_commit,
        regression_evidence=args.regression_evidence,
        output=args.output,
    )
    _emit(args, _result("improvement resolution", **result))
    return 0


def command_improvement_reproduction(args: argparse.Namespace) -> int:
    _lifecycle_project(args)
    result = create_improvement_reproduction(
        args.project_root,
        args.signal,
        args.disposition,
        args.catalog,
        args.resolution,
        args.consumer_receipt,
        consumer_repository=args.consumer_repository,
        reference=args.reference,
        output=args.output,
    )
    _emit(args, _result("improvement reproduction", **result))
    return 0


def command_improvement_attach(args: argparse.Namespace) -> int:
    _lifecycle_project(args)
    result = attach_improvement_chain(
        args.project_root,
        args.change_id,
        args.case_id,
        signal_path=args.signal,
        disposition_path=args.disposition,
        resolution_path=args.resolution,
        reproduction_path=args.reproduction,
        catalog_path=args.catalog,
        actor_id=args.actor,
        context_id=args.context,
        actor_kind=args.actor_kind,
    )
    _emit(args, _result("improvement attach", **result))
    return 0


def command_improvement_ingest(args: argparse.Namespace) -> int:
    _lifecycle_project(args)
    result = ingest_improvement_signal(
        args.project_root,
        args.change_id,
        signal_path=args.signal,
        disposition_path=args.disposition,
        catalog_path=args.catalog,
        actor_id=args.actor,
        context_id=args.context,
        actor_kind=args.actor_kind,
    )
    _emit(args, _result("improvement ingest", **result))
    return 0


def _lifecycle_project(args: argparse.Namespace):
    lock = load_lock(args.project_root)
    issues = synchronized_state(args.project_root, args.process_root, lock)
    if issues:
        raise ContractError("\n".join(issues))
    environment_issues = lifecycle_environment_issues(args.project_root)
    if environment_issues:
        raise ContractError("\n".join(environment_issues))
    path = args.project_root / ".process" / "project.json"
    return validate_project(read_json(path), str(path))


def _change_result(command: str, state: dict[str, Any], **details: Any) -> dict[str, Any]:
    return _result(
        command,
        changeId=state["changeId"],
        phase=state["phase"],
        cycle=state["cycle"],
        revision=state["revision"],
        **details,
    )


def command_change_start(args: argparse.Namespace) -> int:
    project = _lifecycle_project(args)
    state = start_change(
        args.project_root,
        project,
        args.contract,
        actor_id=args.actor,
        context_id=args.context,
        kind=args.actor_kind,
    )
    _emit(
        args,
        _change_result(
            "change start",
            state,
            contract=state["contract"]["path"],
            contractDigest=state["contract"]["digest"],
            comparisonBase=state["comparisonBase"],
        ),
    )
    return 0


def command_change_plan(args: argparse.Namespace) -> int:
    project = _lifecycle_project(args)
    state = register_plan(
        args.project_root,
        project,
        args.change_id,
        args.plan,
        actor_id=args.actor,
        context_id=args.context,
        kind=args.actor_kind,
    )
    _emit(
        args,
        _change_result("change plan", state, plan=state["plan"]["path"]),
    )
    return 0


def command_change_decision_start(args: argparse.Namespace) -> int:
    project = _lifecycle_project(args)
    state, assignment = start_plan_decision_review(
        args.project_root,
        project,
        args.change_id,
        actor_id=args.actor,
        context_id=args.context,
        kind=args.actor_kind,
        method=args.method,
        attested_by=args.attested_by,
        evidence=args.attestation_evidence,
    )
    _emit(
        args,
        _change_result(
            "change decision start",
            state,
            verdict="pending",
            assignment=state["planDecision"]["assignment"]["path"],
            planSha256=assignment["planSha256"],
        ),
    )
    return 0


def command_change_decision_submit(args: argparse.Namespace) -> int:
    project = _lifecycle_project(args)
    state, review = submit_plan_decision_review(
        args.project_root, project, args.change_id, args.review
    )
    _emit(
        args,
        _change_result(
            "change decision submit",
            state,
            verdict=review["verdict"],
            review=state["planDecision"]["review"]["path"],
        ),
    )
    return 0


def command_change_decision_resolve(args: argparse.Namespace) -> int:
    project = _lifecycle_project(args)
    state = resolve_plan_decision(
        args.project_root,
        project,
        args.change_id,
        recommendation_path=args.recommendation,
        assignment_path=args.assignment,
        review_path=args.recommendation_review,
        resolution_path=args.resolution,
        actor_id=args.actor,
        context_id=args.context,
        kind=args.actor_kind,
    )
    _emit(
        args,
        _change_result(
            "change decision resolve",
            state,
            resolution=state["planDecision"]["resolution"]["path"],
        ),
    )
    return 0


def command_change_implement(args: argparse.Namespace) -> int:
    project = _lifecycle_project(args)
    state = begin_implementation(
        args.project_root,
        args.change_id,
        project=project,
        actor_id=args.actor,
        context_id=args.context,
        kind=args.actor_kind,
    )
    _emit(args, _change_result("change implement", state))
    return 0


def command_change_verify(args: argparse.Namespace) -> int:
    project = _lifecycle_project(args)
    state, report = verify_change(
        args.project_root,
        project,
        args.change_id,
        args.profile,
        actor_id=args.actor,
        context_id=args.context,
        kind=args.actor_kind,
    )
    _emit(
        args,
        _change_result(
            "change verify",
            state,
            profile=args.profile,
            checkpoint=report["checkpoint"],
            verificationStatus=report["status"],
            report=next(
                item["path"]
                for item in state["verification"]
                if item["profile"] == args.profile
            ),
        ),
    )
    return 0


def command_change_remote_request(args: argparse.Namespace) -> int:
    project = _lifecycle_project(args)
    state, request = request_remote_verification(
        args.project_root,
        project,
        args.change_id,
        actor_id=args.actor,
        context_id=args.context,
        kind=args.actor_kind,
    )
    _emit(
        args,
        _change_result(
            "change remote request",
            state,
            checkpoint=request["checkpoint"],
            comparisonBase=request["comparisonBase"],
            request=state["remoteVerification"]["request"]["path"],
            requestSha256=canonical_json_digest(request),
        ),
    )
    return 0


def command_change_remote_ingest(args: argparse.Namespace) -> int:
    _lifecycle_project(args)
    state, evidence = ingest_remote_verification(
        args.project_root,
        args.change_id,
        args.evidence,
        actor_id=args.actor,
        context_id=args.context,
        kind=args.actor_kind,
    )
    _emit(
        args,
        _change_result(
            "change remote ingest",
            state,
            checkpoint=evidence["checkpoint"],
            evidence=state["remoteVerification"]["evidence"]["path"],
            artifactCount=len(evidence["artifacts"]),
        ),
    )
    return 0


def command_change_review_start(args: argparse.Namespace) -> int:
    _lifecycle_project(args)
    state, assignment = start_review(
        args.project_root,
        args.change_id,
        actor_id=args.actor,
        context_id=args.context,
        kind=args.actor_kind,
        method=args.method,
        attested_by=args.attested_by,
        evidence=args.attestation_evidence,
    )
    _emit(
        args,
        _change_result(
            "change review start",
            state,
            checkpoint=assignment["checkpoint"],
            comparisonBase=assignment["comparisonBase"],
            request=assignment["path"],
        ),
    )
    return 0


def command_change_review_submit(args: argparse.Namespace) -> int:
    _lifecycle_project(args)
    state = submit_review(args.project_root, args.change_id, args.report)
    _emit(
        args,
        _change_result(
            "change review submit",
            state,
            report=state["review"]["path"],
        ),
    )
    return 0


def command_change_finish(args: argparse.Namespace) -> int:
    _lifecycle_project(args)
    state, completion = finish_change(
        args.project_root,
        args.change_id,
        actor_id=args.actor,
        context_id=args.context,
        kind=args.actor_kind,
    )
    _emit(
        args,
        _change_result(
            "change finish",
            state,
            checkpoint=completion["checkpoint"],
            completion=state["completion"]["path"],
        ),
    )
    return 0


def command_change_status(args: argparse.Namespace) -> int:
    _lifecycle_project(args)
    state = lifecycle_status(args.project_root, args.change_id)
    value = _change_result(
        "change status",
        state,
        status="passed" if state["current"] else "failed",
        current=state["current"],
        issues=state["issues"],
        contract=state["contract"],
        plan=state["plan"],
        implementationActors=state["implementationActors"],
        verification=state["verification"],
        remoteVerification=state.get("remoteVerification"),
        pendingFindings=state["pendingFindings"],
        reviewAssignment=state["reviewAssignment"],
        review=state["review"],
        completion=state["completion"],
    )
    _emit(args, value)
    return 0 if state["current"] else 1


def command_skills_validate(args: argparse.Namespace) -> int:
    issues = validate_skills(args.root)
    try:
        load_process_graph(process_root_from_skills(args.root), args.root)
    except ContractError as error:
        issues.extend(str(error).splitlines())
    value = _result(
        "skills validate",
        status="failed" if issues else "passed",
        root=str(args.root),
        issues=issues,
    )
    _emit(args, value)
    return 1 if issues else 0


def command_digest(args: argparse.Namespace) -> int:
    lock = load_lock(args.project_root) if args.project_root else None
    root = process_skills_root(args.process_root)
    if lock and args.bundle:
        raise ContractError("--project-root and --bundle cannot be combined")
    selected = lock.skills if lock else None
    if args.bundle:
        bundles = load_bundles(args.process_root, root)
        requested = select_bundles(bundles, args.bundle)
        selected = tuple(
            sorted(
                {
                    skill
                    for bundle in requested
                    for skill in bundles[bundle]
                }
            )
        )
    digest = distribution_digest(args.process_root, selected or tuple(
        sorted(path.name for path in root.iterdir() if path.is_dir() and (path / "SKILL.md").is_file())
    ))
    _emit(
        args,
        _result(
            "digest",
            digest=digest,
            skills=list(selected) if selected else sorted(
                path.name
                for path in root.iterdir()
                if path.is_dir() and (path / "SKILL.md").is_file()
            ),
        ),
    )
    return 0


def command_evidence_export(args: argparse.Namespace) -> int:
    details = export_receipt(args.project_root, args.change_id, args.output)
    _emit(args, _result("evidence export", output=str(args.output.resolve()), **details))
    return 0


def command_evidence_export_bootstrap(args: argparse.Namespace) -> int:
    details = export_bootstrap_authorization(
        args.project_root, args.change_id, args.output
    )
    _emit(
        args,
        _result(
            "evidence export-bootstrap",
            output=str(args.output.resolve()),
            **details,
        ),
    )
    return 0


def command_evidence_validate(args: argparse.Namespace) -> int:
    details = validate_receipt(args.receipt)
    _emit(args, _result("evidence validate", receipt=str(args.receipt.resolve()), **details))
    return 0


def command_evidence_validate_bootstrap(args: argparse.Namespace) -> int:
    details = validate_bootstrap_authorization(args.authorization)
    _emit(
        args,
        _result(
            "evidence validate-bootstrap",
            authorization=str(args.authorization.resolve()),
            **details,
        ),
    )
    return 0


def command_evidence_encode_completion(args: argparse.Namespace) -> int:
    details = encode_completion_evidence(
        args.evidence,
        args.output,
        kind=args.evidence_kind,
    )
    _emit(args, _result("evidence encode-completion", **details))
    return 0


def command_evidence_prune(args: argparse.Namespace) -> int:
    details = prune_completed_run(
        args.project_root,
        args.change_id,
        args.receipt,
        apply=args.apply,
    )
    _emit(args, _result("evidence prune", **details))
    return 0


def command_sync(args: argparse.Namespace) -> int:
    issues = sync_skills(
        args.project_root,
        args.process_root,
        check=args.check,
    )
    status = "failed" if issues else "passed"
    _emit(
        args,
        _result(
            "sync check" if args.check else "sync",
            status=status,
            projectRoot=str(args.project_root),
            processRoot=str(args.process_root),
            issues=issues,
        ),
    )
    return 1 if issues else 0


def command_doctor(args: argparse.Namespace) -> int:
    project_path = args.project_root / ".process" / "project.json"
    project = validate_project(read_json(project_path), str(project_path))
    lock = load_lock(args.project_root)
    issues = synchronized_state(args.project_root, args.process_root, lock)
    issues.extend(lifecycle_environment_issues(args.project_root))
    environment = doctor_environment(
        args.project_root,
        project,
        profile=args.profile,
    )
    if environment["status"] == "failed":
        issues.extend(
            f"environment requirement {requirement['id']}: "
            f"{requirement['remediation']}"
            for requirement in environment["requirements"]
            if requirement["status"] != "satisfied"
        )
    value = _result(
        "doctor",
        status="failed" if issues else "passed",
        project=project.identifier,
        processVersion=lock.version,
        profiles=sorted(project.profiles),
        requiredProfiles=list(project.required_profiles),
        skills=list(lock.skills),
        environment=environment,
        issues=issues,
    )
    _emit(args, value)
    return 1 if issues else 0


def command_setup(args: argparse.Namespace) -> int:
    project_path = args.project_root / ".process" / "project.json"
    project = validate_project(read_json(project_path), str(project_path))
    lock = load_lock(args.project_root)
    issues = synchronized_state(args.project_root, args.process_root, lock)
    if issues:
        raise ContractError("\n".join(issues))
    report = setup_environment(
        args.project_root,
        project,
        profile=args.profile,
        apply=args.apply,
        allowed_mutations=set(args.allow),
    )
    _emit(args, _result("setup", **report))
    return 0 if report["status"] in {"passed", "planned"} else 1


def command_exec(args: argparse.Namespace) -> int:
    run = args.run[1:] if args.run[:1] == ["--"] else args.run
    if not run:
        raise ContractError("exec requires a command after `--`")
    if args.timeout_seconds < 1 or args.timeout_seconds > 86_400:
        raise ContractError("--timeout-seconds must be from 1 to 86400")
    lock = load_lock(args.project_root)
    issues = synchronized_state(args.project_root, args.process_root, lock)
    if issues:
        raise ContractError("\n".join(issues))
    project_path = args.project_root / ".process" / "project.json"
    project = validate_project(read_json(project_path), str(project_path))
    require_environment_profile(args.project_root, project, profile=args.profile)
    report = execute_command(
        args.project_root,
        identifier="project-command",
        run=tuple(run),
        timeout_seconds=args.timeout_seconds,
        working_directory=args.working_directory,
        path_entries=environment_path_entries(project, profile=args.profile),
        command_bindings=environment_command_bindings(project, profile=args.profile),
        stream_output=not args.json,
    )
    if args.json:
        _emit(args, _result("exec", status=report["status"], execution=report))
    else:
        _emit(
            args,
            _result(
                "exec",
                status=report["status"],
                exitCode=report["exitCode"],
                durationMs=report["durationMs"],
                commandExecuted=report["command"],
                error=report.get("error"),
            ),
        )
    return 0 if report["status"] == "passed" else 1


def command_verify(args: argparse.Namespace) -> int:
    lock = load_lock(args.project_root)
    integration_issues = synchronized_state(
        args.project_root,
        args.process_root,
        lock,
    )
    if integration_issues:
        raise ContractError("\n".join(integration_issues))
    project_path = args.project_root / ".process" / "project.json"
    project = validate_project(read_json(project_path), str(project_path))
    if args.plan_only:
        plan = plan_profile(
            args.project_root,
            project,
            args.profile,
            base_ref=args.base_ref,
        ).evidence
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        if args.json:
            _write_json(plan)
        else:
            print(f"verify plan {args.profile}: {plan['mode']}")
            if plan["mode"] == "affected-checks":
                print(f"  base: {plan['baseRef']} ({plan['mergeBase']})")
                print(f"  changed paths: {len(plan['changedPaths'])}")
                print(
                    "  affected components: "
                    + (", ".join(plan["affectedComponents"]) or "none")
                )
                print(f"  unmatched paths: {len(plan['unmatchedPaths'])}")
            print(
                "  selected checks: "
                + (", ".join(plan["selectedCheckIds"]) or "none")
            )
            print(
                "  skipped checks: "
                + (", ".join(plan["skippedCheckIds"]) or "none")
            )
            if args.output:
                print(f"  report: {args.output}")
        return 0
    require_environment_profile(args.project_root, project, profile=args.profile)
    report = run_profile(
        args.project_root,
        project,
        args.profile,
        base_ref=args.base_ref,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.json:
        _write_json(report)
    else:
        print(f"verify {args.profile}: {report['status'].upper()}")
        print(f"  project: {report['project']}")
        print(f"  checkpoint: {report['checkpoint'] or 'unavailable'}")
        print(f"  workspace fingerprint: {report['workspaceFingerprint'] or 'unavailable'}")
        print(
            "  source changed during verification: "
            f"{str(report['sourceChangedDuringVerification']).lower()}"
        )
        for check in report["checks"]:
            print(
                f"  {check['id']}: {check['status']} "
                f"({check['durationMs']} ms, exit={check['exitCode']})"
            )
        if args.output:
            print(f"  report: {args.output}")
    return 0 if report["status"] == "passed" else 1


def _publication_result(
    args: argparse.Namespace, command: str, issues: list[str], **details: Any
) -> int:
    _emit(
        args,
        _result(
            command,
            status="failed" if issues else "passed",
            **details,
            issues=issues,
        ),
    )
    return 1 if issues else 0


def command_publication_validate_branch(args: argparse.Namespace) -> int:
    issues = validate_branch(args.branch)
    return _publication_result(
        args,
        "publication validate-branch",
        issues,
        branch=args.branch,
    )


def command_publication_validate_commit(args: argparse.Namespace) -> int:
    issues = validate_commit_subject(args.subject)
    return _publication_result(
        args,
        "publication validate-commit",
        issues,
        subject=args.subject,
    )


def command_publication_validate_range(args: argparse.Namespace) -> int:
    issues, records = validate_commit_range(
        args.project_root,
        branch=args.branch,
        range_spec=args.range_spec,
    )
    return _publication_result(
        args,
        "publication validate-range",
        issues,
        branch=args.branch,
        range=args.range_spec,
        commits=[commit for commit, _subject in records],
    )


def _publication_body(args: argparse.Namespace) -> str:
    if args.body_file is not None:
        try:
            return args.body_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ContractError(f"{args.body_file}: cannot read PR body: {error}") from error
    return os.environ.get("PR_BODY", "")


def _proposal_body(args: argparse.Namespace) -> str:
    if args.body_file is not None:
        try:
            with args.body_file.open("rb") as handle:
                body = handle.read(MAX_PULL_REQUEST_BODY_BYTES + 1)
        except OSError as error:
            raise ContractError(f"{args.body_file}: cannot read PR body: {error}") from error
        if len(body) > MAX_PULL_REQUEST_BODY_BYTES:
            raise ContractError(
                f"{args.body_file}: PR body exceeds "
                f"{MAX_PULL_REQUEST_BODY_BYTES} bytes"
            )
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ContractError(f"{args.body_file}: PR body must be UTF-8") from error
    body = os.environ.get("PR_BODY", "")
    if len(body.encode("utf-8")) > MAX_PULL_REQUEST_BODY_BYTES:
        raise ContractError(
            f"PR_BODY exceeds {MAX_PULL_REQUEST_BODY_BYTES} bytes"
        )
    return body


def command_publication_validate_pr(args: argparse.Namespace) -> int:
    body = _publication_body(args)
    issues = validate_pull_request(
        title=args.title,
        body=body,
        branch=args.branch,
        state=args.state,
    )
    return _publication_result(
        args,
        "publication validate-pr",
        issues,
        branch=args.branch,
        state=args.state,
        title=args.title,
    )


def command_publication_validate_source(args: argparse.Namespace) -> int:
    lifecycle = lifecycle_status(args.project_root, args.change_id)
    issues = validate_completed_publication(
        title=args.title,
        body=_publication_body(args),
        branch=args.branch,
        commit=args.commit,
        lifecycle=lifecycle,
        source=source_state(args.project_root),
    )
    return _publication_result(
        args,
        "publication validate-source",
        issues,
        branch=args.branch,
        changeId=args.change_id,
        commit=args.commit,
        phase=lifecycle["phase"],
        title=args.title,
    )


def command_publication_validate_evidence_source(args: argparse.Namespace) -> int:
    evidence = (
        validate_receipt(args.evidence)
        if args.evidence_kind == "receipt"
        else validate_bootstrap_authorization(args.evidence)
    )
    project_path = args.project_root / ".process" / "project.json"
    project = validate_project(read_json(project_path), str(project_path))
    issues = validate_evidence_publication(
        title=args.title,
        body=_publication_body(args),
        branch=args.branch,
        commit=args.commit,
        project=project.identifier,
        evidence=evidence,
        source=source_state(args.project_root),
    )
    return _publication_result(
        args,
        "publication validate-evidence-source",
        issues,
        branch=args.branch,
        changeId=evidence["changeId"],
        commit=args.commit,
        evidenceKind=args.evidence_kind,
        evidenceSha256=evidence["sha256"],
        title=args.title,
    )


def _proposal_policy_evidence(args: argparse.Namespace):
    return validate_automation_proposal(
        read_json(args.policy_evidence), str(args.policy_evidence)
    )


def command_publication_validate_proposal(args: argparse.Namespace) -> int:
    proposal = _proposal_policy_evidence(args)
    source = source_state(args.project_root)
    issues = validate_controlled_automation_proposal(
        args.project_root,
        repository=args.repository,
        title=args.title,
        body=_proposal_body(args),
        branch=args.branch,
        target_branch=args.target_branch,
        base_commit=args.base_commit,
        state=args.state,
        commit=args.commit,
        verifier_repository=args.verifier_repository,
        verifier_commit=args.verifier_commit,
        proposal=proposal,
        source=source,
    )
    return _publication_result(
        args,
        "publication validate-proposal",
        issues,
        automationOwner=proposal.automation_owner,
        baseSha=args.base_commit,
        branch=args.branch,
        commit=args.commit,
        completionCheck=proposal.completion_check,
        proposalKind=proposal.proposal_kind,
        policySha256=proposal.opt_in_sha256,
        repository=args.repository,
        sourceFingerprint=source.get("fingerprint"),
        targetBranch=args.target_branch,
        verifierCommit=args.verifier_commit,
        verifierRepository=args.verifier_repository,
    )


def command_publication_validate_proposal_completion(
    args: argparse.Namespace,
) -> int:
    proposal = _proposal_policy_evidence(args)
    evidence = validate_receipt(args.evidence)
    project_path = args.project_root / ".process" / "project.json"
    project = validate_project(read_json(project_path), str(project_path))
    body = _proposal_body(args)
    source = source_state(args.project_root)
    issues = validate_controlled_automation_proposal_completion(
        args.project_root,
        repository=args.repository,
        project=project.identifier,
        title=args.title,
        body=body,
        branch=args.branch,
        target_branch=args.target_branch,
        base_commit=args.base_commit,
        commit=args.commit,
        verifier_repository=args.verifier_repository,
        verifier_commit=args.verifier_commit,
        proposal=proposal,
        evidence=evidence,
        source=source,
    )
    return _publication_result(
        args,
        "publication validate-proposal-completion",
        issues,
        baseSha=args.base_commit,
        branch=args.branch,
        changeId=evidence["changeId"],
        commit=args.commit,
        completionCheck=proposal.completion_check,
        evidenceKind=args.evidence_kind,
        evidenceSha256=evidence["sha256"],
        policySha256=proposal.opt_in_sha256,
        repository=args.repository,
        sourceFingerprint=source.get("fingerprint"),
        verifierCommit=args.verifier_commit,
        verifierRepository=args.verifier_repository,
    )


def command_publication_plan_version(args: argparse.Namespace) -> int:
    plan = derive_release_version(args.previous_version, args.change_type)
    _emit(
        args,
        _result(
            "publication plan-version",
            previousVersion=plan.previous_version,
            version=plan.version,
            classification=plan.classification,
            compatibility=plan.compatibility,
            changeTypes=list(plan.change_types),
        ),
    )
    return 0


def command_publication_prepare_release(args: argparse.Namespace) -> int:
    details = prepare_release_candidate(
        args.project_root,
        changes_dir=args.changes_dir,
    )
    _emit(args, _result("publication prepare-release", **details))
    return 0


def command_publication_release_pr_body(args: argparse.Namespace) -> int:
    body = render_release_pull_request(
        args.project_root,
        approved=args.state == "approved",
    )
    try:
        args.output.write_text(body, encoding="utf-8")
    except OSError as error:
        raise ContractError(f"{args.output}: cannot write Release PR body: {error}") from error
    _emit(
        args,
        _result(
            "publication release-pr-body",
            output=str(args.output.resolve()),
            releaseState=args.state,
        ),
    )
    return 0


def command_publication_validate_release(args: argparse.Namespace) -> int:
    details = validate_release_checkpoint(
        args.project_root,
        tag=args.tag,
        release_name=args.release_name,
        commit=args.commit,
        main_ref=args.main_ref,
        receipt_path=args.receipt,
        authorization_path=args.authorization,
        reviewed_commit=args.reviewed_commit,
    )
    _emit(args, _result("publication validate-release", **details))
    return 0


def command_publication_authorize_release(args: argparse.Namespace) -> int:
    details = validate_release_checkpoint(
        args.project_root,
        tag=args.tag,
        release_name=args.release_name,
        commit=args.commit,
        main_ref=args.main_ref,
        receipt_path=args.receipt,
        authorization_path=args.authorization,
        reviewed_commit=args.reviewed_commit,
        require_tag=False,
    )
    _emit(args, _result("publication authorize-release", **details))
    return 0


def command_publication_validate_artifacts(args: argparse.Namespace) -> int:
    details = validate_distribution_attestation(
        args.project_root,
        args.artifacts,
        args.attestation,
        receipt_path=args.receipt,
        authorization_path=args.authorization,
        checkpoint=args.commit,
    )
    _emit(args, _result("publication validate-artifacts", attestation=details))
    return 0


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")


def _add_project_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project-root",
        type=_root,
        default=Path.cwd(),
        help="Consumer project root; defaults to the current directory",
    )


def _add_process_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--process-root",
        type=_root,
        default=default_process_root(),
        help="Engineering-process source root; defaults to the installed source",
    )


def _add_actor(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", required=True, help="Stable actor identity")
    parser.add_argument("--context", required=True, help="Isolated execution context identity")
    parser.add_argument(
        "--actor-kind",
        choices=("agent", "human"),
        required=True,
        help="Actor type",
    )


def _add_lifecycle_common(parser: argparse.ArgumentParser) -> None:
    _add_project_root(parser)
    _add_process_root(parser)
    _add_json(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="processctl",
        description="Agent-neutral engineering process CLI",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    project = commands.add_parser("project", help="Validate project configuration")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_validate = project_commands.add_parser("validate")
    _add_project_root(project_validate)
    _add_json(project_validate)
    project_validate.set_defaults(handler=command_project_validate)
    project_init = project_commands.add_parser(
        "init",
        help="Install the portable process from a validated project manifest",
    )
    _add_project_root(project_init)
    _add_process_root(project_init)
    project_init.add_argument(
        "--manifest",
        type=Path,
        help="Source project manifest; defaults to .process/project.json",
    )
    project_init.add_argument(
        "--bundle",
        action="append",
        default=[],
        help="Select one capability bundle; defaults to core and may be repeated",
    )
    project_init.add_argument(
        "--replace",
        action="store_true",
        help="Replace differing managed project and lock files intentionally",
    )
    _add_json(project_init)
    project_init.set_defaults(handler=command_project_init)

    lock = commands.add_parser("lock", help="Validate process lock")
    lock_commands = lock.add_subparsers(dest="lock_command", required=True)
    lock_validate = lock_commands.add_parser("validate")
    _add_project_root(lock_validate)
    _add_process_root(lock_validate)
    _add_json(lock_validate)
    lock_validate.set_defaults(handler=command_lock_validate)
    lock_create = lock_commands.add_parser("create")
    _add_project_root(lock_create)
    _add_process_root(lock_create)
    lock_create.add_argument(
        "--bundle",
        action="append",
        help="Select one bundle; defaults to core and may be repeated",
    )
    lock_create.add_argument(
        "--replace",
        action="store_true",
        help="Replace an existing lock intentionally",
    )
    _add_json(lock_create)
    lock_create.set_defaults(handler=command_lock_create)

    adoption = commands.add_parser(
        "adoption", help="Apply or validate a hash-locked process-authority adoption"
    )
    adoption_commands = adoption.add_subparsers(
        dest="adoption_command", required=True
    )
    adoption_apply = adoption_commands.add_parser(
        "apply", help="Materialize the proposed authority and all managed assets"
    )
    _add_project_root(adoption_apply)
    _add_process_root(adoption_apply)
    adoption_apply.add_argument(
        "--requirements-lock",
        type=Path,
        required=True,
        help="Hash-locked requirements file or private runner snapshot",
    )
    adoption_apply.add_argument(
        "--requirements-source",
        type=Path,
        help=(
            "Original requirements lock inside the consumer checkout; required "
            "when --requirements-lock is an external private snapshot"
        ),
    )
    adoption_apply.add_argument(
        "--expected-requirements-digest",
        help="Expected sha256 digest of the private requirements snapshot",
    )
    _add_json(adoption_apply)
    adoption_apply.set_defaults(handler=command_adoption_apply)
    adoption_check = adoption_commands.add_parser(
        "check", help="Validate that authority, lock, and managed assets agree"
    )
    _add_project_root(adoption_check)
    _add_process_root(adoption_check)
    adoption_check.add_argument(
        "--requirements-lock",
        type=Path,
        required=True,
        help="Hash-locked requirements file inside the consumer checkout",
    )
    _add_json(adoption_check)
    adoption_check.set_defaults(handler=command_adoption_check)

    contract = commands.add_parser("contract", help="Validate process artifacts")
    contract_commands = contract.add_subparsers(dest="contract_command", required=True)
    contract_validate = contract_commands.add_parser("validate")
    contract_validate.add_argument(
        "--kind",
        choices=(
            "adoption-migration",
            "automation-policy",
            "automation-proposal",
            "automation-proposal-policy",
            "change",
            "improvement-catalog",
            "improvement-disposition",
            "improvement-reproduction",
            "improvement-resolution",
            "improvement-signal",
            "plan",
            "plan-decision-review",
            "plan-decision-review-assignment",
            "recommendation",
            "recommendation-resolution",
            "recommendation-review",
            "recommendation-review-assignment",
            "remote-verification-evidence",
            "remote-verification-request",
            "release",
            "release-change",
            "review",
        ),
        required=True,
    )
    contract_validate.add_argument("path", type=Path)
    _add_json(contract_validate)
    contract_validate.set_defaults(handler=command_contract_validate)

    recommendation = commands.add_parser(
        "recommendation",
        help="Validate evidence-valid recommendations and owner resolutions",
    )
    recommendation_commands = recommendation.add_subparsers(
        dest="recommendation_command", required=True
    )
    recommendation_validate = recommendation_commands.add_parser(
        "validate-chain",
        help="Validate recommendation validity and its independent challenge",
    )
    _add_project_root(recommendation_validate)
    _add_process_root(recommendation_validate)
    recommendation_validate.add_argument(
        "--recommendation", type=Path, required=True
    )
    recommendation_validate.add_argument("--assignment", type=Path, required=True)
    recommendation_validate.add_argument("--review", type=Path, required=True)
    recommendation_validate.add_argument("--resolution", type=Path)
    _add_json(recommendation_validate)
    recommendation_validate.set_defaults(
        handler=command_recommendation_validate_chain
    )
    recommendation_review = recommendation_commands.add_parser(
        "review", help="Register an independent recommendation reviewer"
    )
    recommendation_review_commands = recommendation_review.add_subparsers(
        dest="recommendation_review_command", required=True
    )
    recommendation_review_start = recommendation_review_commands.add_parser(
        "start",
        help="Reserve a fresh project-global context and create an assignment",
    )
    _add_project_root(recommendation_review_start)
    _add_process_root(recommendation_review_start)
    _add_actor(recommendation_review_start)
    recommendation_review_start.add_argument(
        "--recommendation", type=Path, required=True
    )
    recommendation_review_start.add_argument(
        "--method", choices=("isolated-context", "separate-person"), required=True
    )
    recommendation_review_start.add_argument("--attested-by", required=True)
    recommendation_review_start.add_argument(
        "--attestation-evidence", required=True
    )
    _add_json(recommendation_review_start)
    recommendation_review_start.set_defaults(
        handler=command_recommendation_review_start
    )
    recommendation_resolution = recommendation_commands.add_parser(
        "resolution",
        help="Create a non-authorizing owner resolution for an approved chain",
    )
    _add_project_root(recommendation_resolution)
    _add_process_root(recommendation_resolution)
    recommendation_resolution.add_argument(
        "--recommendation", type=Path, required=True
    )
    recommendation_resolution.add_argument("--assignment", type=Path, required=True)
    recommendation_resolution.add_argument("--review", type=Path, required=True)
    recommendation_resolution.add_argument("--selected-option", required=True)
    recommendation_resolution.add_argument("--owner-id", required=True)
    recommendation_resolution.add_argument(
        "--owner-evidence-sha256", required=True
    )
    recommendation_resolution.add_argument(
        "--selection-rationale-sha256", required=True
    )
    recommendation_resolution.add_argument("--output", type=Path, required=True)
    _add_json(recommendation_resolution)
    recommendation_resolution.set_defaults(
        handler=command_recommendation_resolution
    )

    skills = commands.add_parser("skills", help="Validate portable Agent Skills")
    skills_commands = skills.add_subparsers(dest="skills_command", required=True)
    skills_validate = skills_commands.add_parser("validate")
    skills_validate.add_argument(
        "--root",
        type=_root,
        default=process_skills_root(default_process_root()),
    )
    _add_json(skills_validate)
    skills_validate.set_defaults(handler=command_skills_validate)

    digest = commands.add_parser("digest", help="Calculate the selected distribution digest")
    _add_process_root(digest)
    digest.add_argument("--project-root", type=_root)
    digest.add_argument(
        "--bundle",
        action="append",
        help="Select one bundle; repeat to combine bundles",
    )
    _add_json(digest)
    digest.set_defaults(handler=command_digest)

    evidence = commands.add_parser(
        "evidence", help="Export, validate, and explicitly prune lifecycle evidence"
    )
    evidence_commands = evidence.add_subparsers(
        dest="evidence_command", required=True
    )
    evidence_export = evidence_commands.add_parser(
        "export", help="Export one completed lifecycle as a portable receipt"
    )
    _add_project_root(evidence_export)
    evidence_export.add_argument("--change-id", required=True)
    evidence_export.add_argument("--output", type=Path, required=True)
    _add_json(evidence_export)
    evidence_export.set_defaults(handler=command_evidence_export)
    evidence_export_bootstrap = evidence_commands.add_parser(
        "export-bootstrap",
        help="Export a completed lifecycle as one bootstrap authorization bundle",
    )
    _add_project_root(evidence_export_bootstrap)
    evidence_export_bootstrap.add_argument("--change-id", required=True)
    evidence_export_bootstrap.add_argument("--output", type=Path, required=True)
    _add_json(evidence_export_bootstrap)
    evidence_export_bootstrap.set_defaults(
        handler=command_evidence_export_bootstrap
    )
    evidence_validate = evidence_commands.add_parser(
        "validate", help="Validate an exported lifecycle receipt"
    )
    evidence_validate.add_argument("receipt", type=Path)
    _add_json(evidence_validate)
    evidence_validate.set_defaults(handler=command_evidence_validate)
    evidence_validate_bootstrap = evidence_commands.add_parser(
        "validate-bootstrap",
        help="Validate a one-time bootstrap authorization bundle",
    )
    evidence_validate_bootstrap.add_argument("authorization", type=Path)
    _add_json(evidence_validate_bootstrap)
    evidence_validate_bootstrap.set_defaults(
        handler=command_evidence_validate_bootstrap
    )
    evidence_encode_completion = evidence_commands.add_parser(
        "encode-completion",
        help="Validate and encode completion evidence for a publication adapter",
    )
    evidence_encode_completion.add_argument("--evidence", type=Path, required=True)
    evidence_encode_completion.add_argument(
        "--evidence-kind",
        choices=COMPLETION_EVIDENCE_KINDS,
        required=True,
    )
    evidence_encode_completion.add_argument("--output", type=Path, required=True)
    _add_json(evidence_encode_completion)
    evidence_encode_completion.set_defaults(
        handler=command_evidence_encode_completion
    )
    evidence_prune = evidence_commands.add_parser(
        "prune", help="Validate a receipt before pruning a completed local run"
    )
    _add_project_root(evidence_prune)
    evidence_prune.add_argument("--change-id", required=True)
    evidence_prune.add_argument("--receipt", type=Path, required=True)
    evidence_prune.add_argument(
        "--apply",
        action="store_true",
        help="Apply the prune; without this flag only validate and preview",
    )
    _add_json(evidence_prune)
    evidence_prune.set_defaults(handler=command_evidence_prune)

    sync = commands.add_parser("sync", help="Synchronize pinned skills into a project")
    _add_project_root(sync)
    _add_process_root(sync)
    sync.add_argument(
        "--check",
        action="store_true",
        help="Check synchronized state without changing files",
    )
    _add_json(sync)
    sync.set_defaults(handler=command_sync)

    doctor = commands.add_parser("doctor", help="Validate a consumer integration")
    _add_project_root(doctor)
    _add_process_root(doctor)
    doctor.add_argument(
        "--profile",
        help="Environment profile; defaults to environment.defaultProfile",
    )
    _add_json(doctor)
    doctor.set_defaults(handler=command_doctor)

    setup = commands.add_parser(
        "setup",
        help="Plan or explicitly apply project-declared environment setup",
    )
    _add_project_root(setup)
    _add_process_root(setup)
    setup.add_argument(
        "--profile",
        help="Environment profile; defaults to environment.defaultProfile",
    )
    setup.add_argument(
        "--apply",
        action="store_true",
        help="Execute the complete preflighted setup plan",
    )
    setup.add_argument(
        "--allow",
        action="append",
        default=[],
        choices=("host-configuration", "network", "project-files", "user-files"),
        help="Approve one mutation scope for --apply; repeat as needed",
    )
    _add_json(setup)
    setup.set_defaults(handler=command_setup)

    execute = commands.add_parser(
        "exec",
        help="Run one project command with its verified managed-tool environment",
    )
    _add_project_root(execute)
    _add_process_root(execute)
    execute.add_argument("--profile", required=True)
    execute.add_argument("--timeout-seconds", type=int, default=3600)
    execute.add_argument("--working-directory", default=".")
    _add_json(execute)
    execute.add_argument("run", nargs=argparse.REMAINDER)
    execute.set_defaults(handler=command_exec)

    verify = commands.add_parser("verify", help="Run a project-owned verification profile")
    _add_project_root(verify)
    _add_process_root(verify)
    verify.add_argument("--profile", required=True)
    verify.add_argument(
        "--base-ref",
        help="Explicit Git comparison base; lifecycle verification uses its contract base",
    )
    verify.add_argument(
        "--plan-only",
        action="store_true",
        help="Report affected-check selection without probing the environment or running checks",
    )
    verify.add_argument("--output", type=Path)
    _add_json(verify)
    verify.set_defaults(handler=command_verify)

    publication = commands.add_parser(
        "publication", help="Validate portable publication metadata"
    )
    publication_commands = publication.add_subparsers(
        dest="publication_command", required=True
    )

    publication_branch = publication_commands.add_parser(
        "validate-branch", help="Validate a publication branch name"
    )
    publication_branch.add_argument("--branch", required=True)
    _add_json(publication_branch)
    publication_branch.set_defaults(handler=command_publication_validate_branch)

    publication_commit = publication_commands.add_parser(
        "validate-commit", help="Validate a commit subject"
    )
    publication_commit.add_argument("--subject", required=True)
    _add_json(publication_commit)
    publication_commit.set_defaults(handler=command_publication_validate_commit)

    publication_range = publication_commands.add_parser(
        "validate-range", help="Validate a branch and every commit subject in a range"
    )
    _add_project_root(publication_range)
    publication_range.add_argument("--branch", required=True)
    publication_range.add_argument("--range", dest="range_spec", required=True)
    _add_json(publication_range)
    publication_range.set_defaults(handler=command_publication_validate_range)

    publication_pr = publication_commands.add_parser(
        "validate-pr", help="Validate a pull-request title and description"
    )
    publication_pr.add_argument("--title", required=True)
    publication_pr.add_argument("--branch", required=True)
    publication_pr.add_argument("--state", choices=("draft", "ready"), required=True)
    publication_pr.add_argument("--body-file", type=Path)
    _add_json(publication_pr)
    publication_pr.set_defaults(handler=command_publication_validate_pr)

    publication_source = publication_commands.add_parser(
        "validate-source",
        help="Validate source publication against a current completed lifecycle",
    )
    _add_project_root(publication_source)
    publication_source.add_argument("--change-id", required=True)
    publication_source.add_argument("--commit", required=True)
    publication_source.add_argument("--title", required=True)
    publication_source.add_argument("--branch", required=True)
    publication_source.add_argument("--body-file", type=Path)
    _add_json(publication_source)
    publication_source.set_defaults(handler=command_publication_validate_source)

    publication_evidence_source = publication_commands.add_parser(
        "validate-evidence-source",
        help="Validate source publication against external completion evidence",
    )
    _add_project_root(publication_evidence_source)
    publication_evidence_source.add_argument("--evidence", type=Path, required=True)
    publication_evidence_source.add_argument(
        "--evidence-kind",
        choices=("receipt", "bootstrap-authorization"),
        required=True,
    )
    publication_evidence_source.add_argument("--commit", required=True)
    publication_evidence_source.add_argument("--title", required=True)
    publication_evidence_source.add_argument("--branch", required=True)
    publication_evidence_source.add_argument("--body-file", type=Path)
    _add_json(publication_evidence_source)
    publication_evidence_source.set_defaults(
        handler=command_publication_validate_evidence_source
    )

    publication_proposal = publication_commands.add_parser(
        "validate-proposal",
        help="Validate an explicitly opted-in untrusted automation proposal",
    )
    _add_project_root(publication_proposal)
    publication_proposal.add_argument(
        "--policy-evidence", type=Path, required=True
    )
    publication_proposal.add_argument("--repository", required=True)
    publication_proposal.add_argument("--commit", required=True)
    publication_proposal.add_argument("--title", required=True)
    publication_proposal.add_argument("--branch", required=True)
    publication_proposal.add_argument("--target-branch", required=True)
    publication_proposal.add_argument("--base-commit", required=True)
    publication_proposal.add_argument(
        "--state", choices=("draft", "ready"), required=True
    )
    publication_proposal.add_argument("--body-file", type=Path)
    publication_proposal.add_argument("--verifier-repository", required=True)
    publication_proposal.add_argument("--verifier-commit", required=True)
    _add_json(publication_proposal)
    publication_proposal.set_defaults(
        handler=command_publication_validate_proposal
    )

    publication_proposal_completion = publication_commands.add_parser(
        "validate-proposal-completion",
        help="Validate exact proposal policy and lifecycle evidence before merge gating",
    )
    _add_project_root(publication_proposal_completion)
    publication_proposal_completion.add_argument(
        "--policy-evidence", type=Path, required=True
    )
    publication_proposal_completion.add_argument(
        "--evidence", type=Path, required=True
    )
    publication_proposal_completion.add_argument(
        "--evidence-kind",
        choices=("receipt",),
        required=True,
    )
    publication_proposal_completion.add_argument("--repository", required=True)
    publication_proposal_completion.add_argument("--commit", required=True)
    publication_proposal_completion.add_argument("--title", required=True)
    publication_proposal_completion.add_argument("--branch", required=True)
    publication_proposal_completion.add_argument("--target-branch", required=True)
    publication_proposal_completion.add_argument("--base-commit", required=True)
    publication_proposal_completion.add_argument("--body-file", type=Path)
    publication_proposal_completion.add_argument(
        "--verifier-repository", required=True
    )
    publication_proposal_completion.add_argument("--verifier-commit", required=True)
    _add_json(publication_proposal_completion)
    publication_proposal_completion.set_defaults(
        handler=command_publication_validate_proposal_completion
    )

    publication_version = publication_commands.add_parser(
        "plan-version",
        help="Derive the exact next SemVer from public change classifications",
    )
    publication_version.add_argument("--previous-version", required=True)
    publication_version.add_argument(
        "--change-type",
        action="append",
        choices=("fix", "capability", "breaking"),
        required=True,
    )
    _add_json(publication_version)
    publication_version.set_defaults(handler=command_publication_plan_version)

    publication_prepare = publication_commands.add_parser(
        "prepare-release",
        help="Materialize one deterministic Release PR candidate from change fragments",
    )
    _add_project_root(publication_prepare)
    publication_prepare.add_argument(
        "--changes-dir",
        type=Path,
        help="Release change directory; defaults to <project-root>/release-changes",
    )
    _add_json(publication_prepare)
    publication_prepare.set_defaults(handler=command_publication_prepare_release)

    publication_body = publication_commands.add_parser(
        "release-pr-body",
        help="Render the managed draft or approved Release PR body",
    )
    _add_project_root(publication_body)
    publication_body.add_argument(
        "--state", choices=("draft", "approved"), required=True
    )
    publication_body.add_argument("--output", type=Path, required=True)
    _add_json(publication_body)
    publication_body.set_defaults(handler=command_publication_release_pr_body)

    publication_release = publication_commands.add_parser(
        "validate-release",
        help="Validate a release contract, immutable tag, and main ancestry",
    )
    _add_project_root(publication_release)
    publication_release.add_argument("--tag", required=True)
    publication_release.add_argument("--release-name", required=True)
    publication_release.add_argument("--commit", required=True)
    publication_release.add_argument("--main-ref", default="origin/main")
    publication_release.add_argument("--receipt", type=Path)
    publication_release.add_argument("--authorization", type=Path)
    publication_release.add_argument("--reviewed-commit")
    _add_json(publication_release)
    publication_release.set_defaults(handler=command_publication_validate_release)

    publication_authorize = publication_commands.add_parser(
        "authorize-release",
        help="Validate reviewed release evidence and merge identity before tag creation",
    )
    _add_project_root(publication_authorize)
    publication_authorize.add_argument("--tag", required=True)
    publication_authorize.add_argument("--release-name", required=True)
    publication_authorize.add_argument("--commit", required=True)
    publication_authorize.add_argument("--main-ref", default="origin/main")
    publication_authorize.add_argument("--receipt", type=Path)
    publication_authorize.add_argument("--authorization", type=Path)
    publication_authorize.add_argument("--reviewed-commit", required=True)
    _add_json(publication_authorize)
    publication_authorize.set_defaults(handler=command_publication_authorize_release)

    publication_artifacts = publication_commands.add_parser(
        "validate-artifacts",
        help="Validate distribution digests against release and lifecycle evidence",
    )
    _add_project_root(publication_artifacts)
    publication_artifacts.add_argument("--artifacts", type=_root, required=True)
    publication_artifacts.add_argument("--attestation", type=Path, required=True)
    publication_artifacts.add_argument("--receipt", type=Path)
    publication_artifacts.add_argument("--authorization", type=Path)
    publication_artifacts.add_argument("--commit", required=True)
    _add_json(publication_artifacts)
    publication_artifacts.set_defaults(
        handler=command_publication_validate_artifacts
    )

    improvement = commands.add_parser(
        "improvement",
        help="Validate and govern federated process-improvement artifacts",
    )
    improvement_commands = improvement.add_subparsers(
        dest="improvement_command", required=True
    )
    for name, handler, help_text in (
        (
            "validate-chain",
            command_improvement_validate_chain,
            "Validate an immutable signal, disposition, resolution, and reproduction chain",
        ),
        (
            "status",
            command_improvement_status,
            "Report the current portable improvement-chain phase and next owner",
        ),
    ):
        chain = improvement_commands.add_parser(name, help=help_text)
        chain.add_argument("--signal", type=Path, required=True)
        chain.add_argument("--disposition", type=Path)
        chain.add_argument("--resolution", type=Path)
        chain.add_argument("--reproduction", type=Path)
        chain.add_argument("--catalog", type=Path)
        _add_json(chain)
        chain.set_defaults(handler=handler)

    improvement_classify = improvement_commands.add_parser(
        "classify",
        help="Classify one observed lifecycle failure before corrective work continues",
    )
    _add_lifecycle_common(improvement_classify)
    _add_actor(improvement_classify)
    improvement_classify.add_argument("--change-id", required=True)
    improvement_classify.add_argument("--case-id", required=True)
    improvement_classify.add_argument(
        "--owner-boundary",
        choices=(
            "missing-product-or-authorization-input",
            "operations-or-external",
            "project-local",
            "shared-process",
        ),
        required=True,
    )
    improvement_classify.add_argument(
        "--reusable-class",
        choices=(
            "deterministic-enforcement",
            "local-behavior",
            "obsolete-guidance",
            "portability-gap",
            "process-rule",
        ),
        required=True,
    )
    improvement_classify.add_argument("--invariant-id", required=True)
    improvement_classify.add_argument(
        "--disposition",
        choices=(
            "external-recovery",
            "input-required",
            "local-fix",
            "producer-improvement",
            "shared-escalation",
        ),
        required=True,
    )
    improvement_classify.add_argument("--rationale-sha256", required=True)
    improvement_classify.add_argument("--target-project")
    improvement_classify.add_argument("--target-repository")
    improvement_classify.set_defaults(handler=command_improvement_classify)

    improvement_observe = improvement_commands.add_parser(
        "observe",
        help="Export a bounded external or supplemental failure as an untrusted signal",
    )
    _add_lifecycle_common(improvement_observe)
    improvement_observe.add_argument("--signal-id", required=True)
    improvement_observe.add_argument("--source-repository", required=True)
    improvement_observe.add_argument("--target-project", required=True)
    improvement_observe.add_argument("--target-repository", required=True)
    improvement_observe.add_argument(
        "--trigger-kind",
        choices=(
            "external-integration",
            "repeated-friction",
            "review-finding",
            "verification-failure",
        ),
        required=True,
    )
    improvement_observe.add_argument(
        "--trigger-status",
        choices=("blocked", "changes-requested", "failed", "timed-out"),
        required=True,
    )
    improvement_observe.add_argument(
        "--owner-boundary",
        choices=(
            "missing-product-or-authorization-input",
            "operations-or-external",
            "project-local",
            "shared-process",
        ),
        required=True,
    )
    improvement_observe.add_argument(
        "--reusable-class",
        choices=(
            "deterministic-enforcement",
            "local-behavior",
            "obsolete-guidance",
            "portability-gap",
            "process-rule",
        ),
        required=True,
    )
    improvement_observe.add_argument("--invariant-id", required=True)
    improvement_observe.add_argument("--rationale-sha256", required=True)
    improvement_observe.add_argument(
        "--affected-surface", action="append", required=True
    )
    improvement_observe.add_argument(
        "--evidence-kind",
        choices=(
            "external-event",
            "review-report",
            "supplemental-verification",
            "verification-report",
        ),
        required=True,
    )
    improvement_observe.add_argument("--evidence", type=Path, required=True)
    improvement_observe.add_argument("--reference")
    improvement_observe.add_argument("--change-id")
    improvement_observe.add_argument("--cycle", type=int)
    improvement_observe.add_argument("--output", type=Path, required=True)
    improvement_observe.set_defaults(handler=command_improvement_observe)

    improvement_export = improvement_commands.add_parser(
        "export-signal",
        help="Export one classified shared consumer case as an untrusted portable signal",
    )
    _add_lifecycle_common(improvement_export)
    _add_actor(improvement_export)
    improvement_export.add_argument("--change-id", required=True)
    improvement_export.add_argument("--case-id", required=True)
    improvement_export.add_argument("--source-repository", required=True)
    improvement_export.add_argument(
        "--affected-surface",
        action="append",
        required=True,
    )
    improvement_export.add_argument("--reference")
    improvement_export.add_argument("--output", type=Path, required=True)
    improvement_export.set_defaults(handler=command_improvement_export_signal)

    improvement_disposition = improvement_commands.add_parser(
        "disposition",
        help="Create a producer-owned triage artifact for one untrusted signal",
    )
    _add_lifecycle_common(improvement_disposition)
    improvement_disposition.add_argument("--signal", type=Path, required=True)
    improvement_disposition.add_argument("--catalog", type=Path, required=True)
    improvement_disposition.add_argument("--producer-repository", required=True)
    improvement_disposition.add_argument(
        "--decision",
        choices=("accepted", "duplicate", "rejected"),
        required=True,
    )
    improvement_disposition.add_argument(
        "--owner-boundary",
        choices=(
            "missing-product-or-authorization-input",
            "operations-or-external",
            "project-local",
            "shared-process",
        ),
        required=True,
    )
    improvement_disposition.add_argument(
        "--reusable-class",
        choices=(
            "deterministic-enforcement",
            "local-behavior",
            "obsolete-guidance",
            "portability-gap",
            "process-rule",
        ),
        required=True,
    )
    improvement_disposition.add_argument("--invariant-id", required=True)
    improvement_disposition.add_argument("--linked-change-id")
    improvement_disposition.add_argument("--rationale-sha256", required=True)
    improvement_disposition.add_argument("--exception-approved-by")
    improvement_disposition.add_argument("--exception-evidence-sha256")
    improvement_disposition.add_argument("--output", type=Path, required=True)
    improvement_disposition.set_defaults(handler=command_improvement_disposition)

    improvement_resolution = improvement_commands.add_parser(
        "resolution",
        help="Bind producer completion and an immutable release to an accepted signal",
    )
    _add_project_root(improvement_resolution)
    improvement_resolution.add_argument("--signal", type=Path, required=True)
    improvement_resolution.add_argument("--disposition", type=Path, required=True)
    improvement_resolution.add_argument("--catalog", type=Path, required=True)
    improvement_resolution.add_argument(
        "--lifecycle-receipt", type=Path, required=True
    )
    improvement_resolution.add_argument("--release-contract", type=Path, required=True)
    improvement_resolution.add_argument("--release-receipt", type=Path)
    improvement_resolution.add_argument("--release-authorization", type=Path)
    improvement_resolution.add_argument("--release-repository", required=True)
    improvement_resolution.add_argument("--release-tag", required=True)
    improvement_resolution.add_argument("--release-name", required=True)
    improvement_resolution.add_argument("--release-commit", required=True)
    improvement_resolution.add_argument("--artifact-root", type=_root, required=True)
    improvement_resolution.add_argument(
        "--artifact-attestation", type=Path, required=True
    )
    improvement_resolution.add_argument(
        "--regression-evidence", action="append", required=True
    )
    improvement_resolution.add_argument("--output", type=Path, required=True)
    _add_json(improvement_resolution)
    improvement_resolution.set_defaults(handler=command_improvement_resolution)

    improvement_reproduction = improvement_commands.add_parser(
        "reproduction",
        help="Bind a released producer correction to passing consumer evidence",
    )
    _add_lifecycle_common(improvement_reproduction)
    improvement_reproduction.add_argument("--signal", type=Path, required=True)
    improvement_reproduction.add_argument(
        "--disposition", type=Path, required=True
    )
    improvement_reproduction.add_argument("--catalog", type=Path, required=True)
    improvement_reproduction.add_argument("--resolution", type=Path, required=True)
    improvement_reproduction.add_argument(
        "--consumer-receipt", type=Path, required=True
    )
    improvement_reproduction.add_argument("--consumer-repository", required=True)
    improvement_reproduction.add_argument("--reference")
    improvement_reproduction.add_argument("--output", type=Path, required=True)
    improvement_reproduction.set_defaults(handler=command_improvement_reproduction)

    improvement_attach = improvement_commands.add_parser(
        "attach",
        help="Bind a validated producer chain to one consumer improvement case",
    )
    _add_lifecycle_common(improvement_attach)
    _add_actor(improvement_attach)
    improvement_attach.add_argument("--change-id", required=True)
    improvement_attach.add_argument("--case-id", required=True)
    improvement_attach.add_argument("--signal", type=Path, required=True)
    improvement_attach.add_argument("--disposition", type=Path)
    improvement_attach.add_argument("--resolution", type=Path)
    improvement_attach.add_argument("--reproduction", type=Path)
    improvement_attach.add_argument("--catalog", type=Path, required=True)
    improvement_attach.set_defaults(handler=command_improvement_attach)

    improvement_ingest = improvement_commands.add_parser(
        "ingest",
        help="Register an accepted external signal in its linked producer lifecycle",
    )
    _add_lifecycle_common(improvement_ingest)
    _add_actor(improvement_ingest)
    improvement_ingest.add_argument("--change-id", required=True)
    improvement_ingest.add_argument("--signal", type=Path, required=True)
    improvement_ingest.add_argument("--disposition", type=Path, required=True)
    improvement_ingest.add_argument("--catalog", type=Path, required=True)
    improvement_ingest.set_defaults(handler=command_improvement_ingest)

    change = commands.add_parser("change", help="Run the canonical change lifecycle")
    change_commands = change.add_subparsers(dest="change_command", required=True)

    change_start = change_commands.add_parser("start", help="Register a change specification")
    _add_lifecycle_common(change_start)
    _add_actor(change_start)
    change_start.add_argument("--contract", type=Path, required=True)
    change_start.set_defaults(handler=command_change_start)

    change_plan = change_commands.add_parser("plan", help="Register an implementation plan")
    _add_lifecycle_common(change_plan)
    _add_actor(change_plan)
    change_plan.add_argument("--change-id", required=True)
    change_plan.add_argument("--plan", type=Path, required=True)
    change_plan.set_defaults(handler=command_change_plan)

    change_decision = change_commands.add_parser(
        "decision", help="Assess material decisions in an authored plan"
    )
    decision_commands = change_decision.add_subparsers(
        dest="change_decision_command", required=True
    )
    decision_start = decision_commands.add_parser(
        "start", help="Reserve a fresh reviewer and create the plan assessment assignment"
    )
    _add_lifecycle_common(decision_start)
    _add_actor(decision_start)
    decision_start.add_argument("--change-id", required=True)
    decision_start.add_argument(
        "--method", choices=("isolated-context", "separate-person"), required=True
    )
    decision_start.add_argument("--attested-by", required=True)
    decision_start.add_argument("--attestation-evidence", required=True)
    decision_start.set_defaults(handler=command_change_decision_start)

    decision_submit = decision_commands.add_parser(
        "submit", help="Validate and register the fresh material-decision assessment"
    )
    _add_lifecycle_common(decision_submit)
    decision_submit.add_argument("--change-id", required=True)
    decision_submit.add_argument("--review", type=Path, required=True)
    decision_submit.set_defaults(handler=command_change_decision_submit)

    decision_resolve = decision_commands.add_parser(
        "resolve", help="Bind an approved recommendation and owner resolution"
    )
    _add_lifecycle_common(decision_resolve)
    _add_actor(decision_resolve)
    decision_resolve.add_argument("--change-id", required=True)
    decision_resolve.add_argument("--recommendation", type=Path, required=True)
    decision_resolve.add_argument("--assignment", type=Path, required=True)
    decision_resolve.add_argument(
        "--recommendation-review", type=Path, required=True
    )
    decision_resolve.add_argument("--resolution", type=Path, required=True)
    decision_resolve.set_defaults(handler=command_change_decision_resolve)

    change_implement = change_commands.add_parser(
        "implement", help="Register an implementation actor and begin a cycle"
    )
    _add_lifecycle_common(change_implement)
    _add_actor(change_implement)
    change_implement.add_argument("--change-id", required=True)
    change_implement.set_defaults(handler=command_change_implement)

    change_verify = change_commands.add_parser(
        "verify", help="Run and bind a required verification profile"
    )
    _add_lifecycle_common(change_verify)
    _add_actor(change_verify)
    change_verify.add_argument("--change-id", required=True)
    change_verify.add_argument("--profile", required=True)
    change_verify.set_defaults(handler=command_change_verify)

    change_remote = change_commands.add_parser(
        "remote", help="Bind required exact-checkpoint remote verification"
    )
    remote_commands = change_remote.add_subparsers(
        dest="change_remote_command", required=True
    )
    remote_request = remote_commands.add_parser(
        "request", help="Create an exact no-authority remote verification request"
    )
    _add_lifecycle_common(remote_request)
    _add_actor(remote_request)
    remote_request.add_argument("--change-id", required=True)
    remote_request.set_defaults(handler=command_change_remote_request)

    remote_ingest = remote_commands.add_parser(
        "ingest", help="Validate and bind a complete remote evidence set"
    )
    _add_lifecycle_common(remote_ingest)
    _add_actor(remote_ingest)
    remote_ingest.add_argument("--change-id", required=True)
    remote_ingest.add_argument("--evidence", type=Path, required=True)
    remote_ingest.set_defaults(handler=command_change_remote_ingest)

    change_review = change_commands.add_parser(
        "review", help="Run the independent-review gate"
    )
    review_commands = change_review.add_subparsers(
        dest="change_review_command", required=True
    )
    review_start = review_commands.add_parser(
        "start", help="Register an independent reviewer and immutable assignment"
    )
    _add_lifecycle_common(review_start)
    _add_actor(review_start)
    review_start.add_argument("--change-id", required=True)
    review_start.add_argument(
        "--method", choices=("isolated-context", "separate-person"), required=True
    )
    review_start.add_argument("--attested-by", required=True)
    review_start.add_argument("--attestation-evidence", required=True)
    review_start.set_defaults(handler=command_change_review_start)

    review_submit = review_commands.add_parser(
        "submit", help="Validate and register the independent review report"
    )
    _add_lifecycle_common(review_submit)
    review_submit.add_argument("--change-id", required=True)
    review_submit.add_argument("--report", type=Path, required=True)
    review_submit.set_defaults(handler=command_change_review_submit)

    change_finish = change_commands.add_parser(
        "finish", help="Complete an approved current change"
    )
    _add_lifecycle_common(change_finish)
    _add_actor(change_finish)
    change_finish.add_argument("--change-id", required=True)
    change_finish.set_defaults(handler=command_change_finish)

    change_status = change_commands.add_parser("status", help="Inspect lifecycle state")
    _add_lifecycle_common(change_status)
    change_status.add_argument("--change-id", required=True)
    change_status.set_defaults(handler=command_change_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except ContractError as error:
        value = _result(args.command, status="failed", errors=str(error).splitlines())
        if getattr(args, "json", False):
            _write_json(value)
        else:
            print(f"{args.command}: FAILED", file=sys.stderr)
            for line in str(error).splitlines():
                print(f"  {line}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"{args.command}: INTERRUPTED", file=sys.stderr)
        return 130
