from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from . import VERSION
from .bundles import load_bundles, select_bundles
from .bootstrap import initialize_project
from .contracts import (
    ContractError,
    read_json,
    validate_change,
    validate_plan,
    validate_process_lock,
    validate_project,
    validate_review,
)
from .distribution import distribution_digest
from .environment import (
    doctor_environment,
    require_environment_profile,
    setup_environment,
)
from .lifecycle import (
    begin_implementation,
    finish_change,
    lifecycle_environment_issues,
    lifecycle_status,
    register_plan,
    start_change,
    start_review,
    submit_review,
    verify_change,
)
from .runner import run_profile
from .publication import (
    validate_branch,
    validate_commit_range,
    validate_commit_subject,
    validate_pull_request,
)
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
            environmentProfiles=(
                sorted(project.environment.profiles) if project.environment else []
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


def command_contract_validate(args: argparse.Namespace) -> int:
    document = read_json(args.path)
    validators: dict[str, Callable[[Any, str], None]] = {
        "change": validate_change,
        "plan": validate_plan,
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


def command_change_implement(args: argparse.Namespace) -> int:
    _lifecycle_project(args)
    state = begin_implementation(
        args.project_root,
        args.change_id,
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
        pendingFindings=state["pendingFindings"],
        reviewAssignment=state["reviewAssignment"],
        review=state["review"],
        completion=state["completion"],
    )
    _emit(args, value)
    return 0 if state["current"] else 1


def command_skills_validate(args: argparse.Namespace) -> int:
    issues = validate_skills(args.root)
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
    require_environment_profile(args.project_root, project, profile=args.profile)
    report = run_profile(args.project_root, project, args.profile)
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


def command_publication_validate_pr(args: argparse.Namespace) -> int:
    if args.body_file is not None:
        try:
            body = args.body_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ContractError(f"{args.body_file}: cannot read PR body: {error}") from error
    else:
        body = os.environ.get("PR_BODY", "")
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

    contract = commands.add_parser("contract", help="Validate process artifacts")
    contract_commands = contract.add_subparsers(dest="contract_command", required=True)
    contract_validate = contract_commands.add_parser("validate")
    contract_validate.add_argument(
        "--kind", choices=("change", "plan", "review"), required=True
    )
    contract_validate.add_argument("path", type=Path)
    _add_json(contract_validate)
    contract_validate.set_defaults(handler=command_contract_validate)

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

    verify = commands.add_parser("verify", help="Run a project-owned verification profile")
    _add_project_root(verify)
    _add_process_root(verify)
    verify.add_argument("--profile", required=True)
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
