"""Public command-line interface for engineering-process."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Callable

from . import VERSION
from .adoption import apply_adoption, check_adoption
from .commands import run_check, run_profile
from .contracts import (
    CONTRACT_KINDS,
    ProcessError,
    load_and_validate,
    read_json,
    validate_document,
)
from .distribution import (
    distribution_digest,
    distribution_root,
    schemas_root,
    skills_root,
)
from .lifecycle import (
    begin_implementation,
    finish_change,
    lifecycle_status,
    register_plan,
    start_change,
    start_review,
    submit_review,
    verify_change,
)
from .project import load_project
from .publication_compat import (
    branch_issues,
    commit_issues,
    validate_pull_request,
    validate_range,
)
from .release import validate_release
from .repository import repository_snapshot, same_checkpoint
from .skills import validate_skills


Result = tuple[dict[str, Any], int]


def _root(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"directory does not exist: {value}")
    return path


def _process_root(args: argparse.Namespace) -> Path:
    return distribution_root(args.process_root)


def _result(command: str, status: str = "passed", **details: Any) -> dict[str, Any]:
    return {"command": command, "status": status, **details}


def _state_result(command: str, state: dict[str, Any], **details: Any) -> dict[str, Any]:
    return _result(
        command,
        changeId=state["changeId"],
        phase=state["phase"],
        cycle=state["cycle"],
        **details,
    )


def _emit(value: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(f"{value['command']}: {str(value['status']).upper()}")
    for key, item in value.items():
        if key in {"command", "status"}:
            continue
        if isinstance(item, (dict, list)):
            rendered = json.dumps(item, ensure_ascii=False, sort_keys=True)
        else:
            rendered = str(item)
        print(f"  {key}: {rendered}")


def command_project_validate(args: argparse.Namespace) -> Result:
    process_root = _process_root(args)
    project = load_project(args.project_root, process_root)
    return _result(
        "project validate",
        project=project["project"],
        profiles=sorted(project["profiles"]),
        requiredProfiles=project["lifecycle"]["requiredProfiles"],
    ), 0


def command_lock_validate(args: argparse.Namespace) -> Result:
    process_root = _process_root(args)
    path = args.project_root / ".process" / "process.lock"
    lock = load_and_validate(
        path, "process-lock", schema_root=schemas_root(process_root)
    )
    return _result(
        "lock validate",
        version=lock["process"]["version"],
        digest=lock["process"]["digest"],
        skills=lock["skills"],
    ), 0


def command_contract_validate(args: argparse.Namespace) -> Result:
    process_root = _process_root(args)
    load_and_validate(args.path, args.kind, schema_root=schemas_root(process_root))
    return _result("contract validate", kind=args.kind, path=str(args.path)), 0


def command_skills_validate(args: argparse.Namespace) -> Result:
    process_root = _process_root(args)
    root = args.root.resolve() if args.root else skills_root(process_root)
    details = validate_skills(root, process_root=process_root)
    return _result("skills validate", root=str(root), **details), 0


def command_doctor(args: argparse.Namespace) -> Result:
    process_root = _process_root(args)
    project = load_project(args.project_root, process_root)
    if args.profile is not None and args.profile not in project["profiles"]:
        raise ProcessError(f"unknown verification profile: {args.profile}")
    lock_path = args.project_root / ".process" / "process.lock"
    lock = load_and_validate(
        lock_path, "process-lock", schema_root=schemas_root(process_root)
    )
    issues: list[str] = []
    if lock["process"]["version"] != VERSION:
        issues.append(
            f"process lock pins {lock['process']['version']}, running process is {VERSION}"
        )
    actual_digest = distribution_digest(process_root)
    if lock["process"]["digest"] != actual_digest:
        issues.append("process lock digest does not match the running distribution")
    requirements = args.project_root / "requirements" / "process.txt"
    if requirements.is_file() and not issues:
        adoption = check_adoption(args.project_root, process_root, requirements)
        issues.extend(adoption["issues"])
    status = "passed" if not issues else "failed"
    return _result(
        "doctor",
        status=status,
        project=project["project"],
        processVersion=VERSION,
        profiles=sorted(project["profiles"]),
        issues=issues,
    ), (0 if not issues else 1)


def command_setup(args: argparse.Namespace) -> Result:
    """Run the consumer-owned setup actions retained during legacy migration."""
    project = load_project(args.project_root, _process_root(args))
    if args.profile not in project["profiles"]:
        raise ProcessError(f"unknown verification profile: {args.profile}")
    actions = project.get("setup", [])
    if not args.apply:
        return _result(
            "setup",
            profile=args.profile,
            actions=[action["id"] for action in actions],
            applied=False,
        ), 0
    reports: list[dict[str, Any]] = []
    for action in actions:
        report = run_check(args.project_root, action)
        reports.append(report)
        if report["status"] != "passed":
            break
    status = "passed" if len(reports) == len(actions) and all(
        report["status"] == "passed" for report in reports
    ) else "failed"
    return _result(
        "setup",
        status=status,
        profile=args.profile,
        actions=reports,
        applied=True,
    ), (0 if status == "passed" else 1)


def command_verify(args: argparse.Namespace) -> Result:
    process_root = _process_root(args)
    project = load_project(args.project_root, process_root)
    before = repository_snapshot(args.project_root)
    report = run_profile(args.project_root, project, args.profile)
    after = repository_snapshot(args.project_root)
    if not same_checkpoint(before, after):
        report["status"] = "failed"
        report["reason"] = "repository changed while verification was running"
    report["checkpoint"] = after
    return _result("verify", status=report["status"], report=report), (
        0 if report["status"] == "passed" else 1
    )


def command_adoption_apply(args: argparse.Namespace) -> Result:
    process_root = _process_root(args)
    details = apply_adoption(
        args.project_root,
        process_root,
        args.requirements_lock,
        requirements_source=args.requirements_source,
        expected_requirements_digest=args.expected_requirements_digest,
    )
    return _result("adoption apply", **details), 0


def command_adoption_check(args: argparse.Namespace) -> Result:
    process_root = _process_root(args)
    details = check_adoption(
        args.project_root, process_root, args.requirements_lock
    )
    code = 0 if details["status"] == "passed" else 1
    return _result("adoption check", **details), code


def command_change_start(args: argparse.Namespace) -> Result:
    process_root = _process_root(args)
    project = load_project(args.project_root, process_root)
    state = start_change(
        args.project_root,
        process_root,
        project,
        args.contract,
        actor_id=args.actor,
        context_id=args.context,
        kind=args.actor_kind,
    )
    return _state_result(
        "change start", state, contractDigest=state["contract"]["digest"]
    ), 0


def command_change_plan(args: argparse.Namespace) -> Result:
    process_root = _process_root(args)
    state = register_plan(
        args.project_root,
        process_root,
        args.change_id,
        args.plan,
        actor_id=args.actor,
        context_id=args.context,
        kind=args.actor_kind,
    )
    return _state_result("change plan", state, planDigest=state["plan"]["digest"]), 0


def command_change_implement(args: argparse.Namespace) -> Result:
    process_root = _process_root(args)
    state = begin_implementation(
        args.project_root,
        process_root,
        args.change_id,
        actor_id=args.actor,
        context_id=args.context,
        kind=args.actor_kind,
    )
    return _state_result("change implement", state), 0


def command_change_verify(args: argparse.Namespace) -> Result:
    process_root = _process_root(args)
    project = load_project(args.project_root, process_root)
    state, report = verify_change(
        args.project_root, process_root, project, args.change_id, args.profile
    )
    code = 0 if report["status"] == "passed" else 1
    return _state_result(
        "change verify", state, profile=args.profile, profileStatus=report["status"]
    ), code


def command_change_review_start(args: argparse.Namespace) -> Result:
    process_root = _process_root(args)
    state = start_review(
        args.project_root,
        process_root,
        args.change_id,
        actor_id=args.actor,
        context_id=args.context,
        kind=args.actor_kind,
    )
    return _state_result(
        "change review start",
        state,
        assignment=state["reviewAssignment"],
        reportPath=f".process/runs/{args.change_id}/review-{state['cycle']}.json",
    ), 0


def command_change_review_submit(args: argparse.Namespace) -> Result:
    process_root = _process_root(args)
    state = submit_review(
        args.project_root, process_root, args.change_id, args.review
    )
    return _state_result(
        "change review submit",
        state,
        verdict=state["review"]["document"]["verdict"],
    ), 0


def command_change_finish(args: argparse.Namespace) -> Result:
    process_root = _process_root(args)
    state, receipt = finish_change(
        args.project_root,
        process_root,
        args.change_id,
        actor_id=args.actor,
        context_id=args.context,
        kind=args.actor_kind,
    )
    return _state_result(
        "change finish", state, receipt=state["receipt"], checkpoint=receipt["checkpoint"]
    ), 0


def command_change_status(args: argparse.Namespace) -> Result:
    process_root = _process_root(args)
    state = lifecycle_status(args.project_root, process_root, args.change_id)
    return _state_result(
        "change status",
        state,
        nextCommand=state["nextCommand"],
        verification={
            name: report["status"] for name, report in state["verification"].items()
        },
    ), 0


def command_release_validate(args: argparse.Namespace) -> Result:
    details = validate_release(
        args.project_root, _process_root(args), tag=args.tag
    )
    return _result("release validate", **details), 0


def _publication_result(command: str, details: dict[str, Any]) -> Result:
    issues = details["issues"]
    status = "passed" if not issues else "failed"
    return _result(command, status=status, **details), (0 if not issues else 1)


def command_publication_branch(args: argparse.Namespace) -> Result:
    return _publication_result(
        "publication validate-branch", {"issues": branch_issues(args.branch)}
    )


def command_publication_commit(args: argparse.Namespace) -> Result:
    return _publication_result(
        "publication validate-commit", {"issues": commit_issues(args.subject)}
    )


def command_publication_range(args: argparse.Namespace) -> Result:
    return _publication_result(
        "publication validate-range",
        validate_range(args.project_root, args.branch, args.range_spec),
    )


def command_publication_pr(args: argparse.Namespace) -> Result:
    return _publication_result(
        "publication validate-pr",
        validate_pull_request(
            title=args.title,
            branch=args.branch,
            state=args.state,
            body_path=args.body_file,
        ),
    )


def _add_common(parser: argparse.ArgumentParser, *, project: bool = True) -> None:
    if project:
        parser.add_argument("--project-root", type=_root, default=Path.cwd())
    parser.add_argument("--process-root", type=_root)
    parser.add_argument("--json", action="store_true")


def _add_actor(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--actor-kind", choices=("agent", "human"), default="agent")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="processctl", description="Small, enforceable engineering process"
    )
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)

    project = commands.add_parser("project", help="Validate consumer configuration")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_validate = project_commands.add_parser("validate")
    _add_common(project_validate)
    project_validate.set_defaults(handler=command_project_validate)

    lock = commands.add_parser("lock", help="Validate the adopted process lock")
    lock_commands = lock.add_subparsers(dest="lock_command", required=True)
    lock_validate = lock_commands.add_parser("validate")
    _add_common(lock_validate)
    lock_validate.set_defaults(handler=command_lock_validate)

    contract = commands.add_parser("contract", help="Validate a JSON contract")
    contract_commands = contract.add_subparsers(dest="contract_command", required=True)
    contract_validate = contract_commands.add_parser("validate")
    contract_validate.add_argument("--kind", choices=CONTRACT_KINDS, required=True)
    contract_validate.add_argument("path", type=Path)
    _add_common(contract_validate, project=False)
    contract_validate.set_defaults(handler=command_contract_validate)

    skills = commands.add_parser("skills", help="Validate the complete skill graph")
    skills_commands = skills.add_subparsers(dest="skills_command", required=True)
    skills_validate = skills_commands.add_parser("validate")
    skills_validate.add_argument("--root", type=Path)
    _add_common(skills_validate, project=False)
    skills_validate.set_defaults(handler=command_skills_validate)

    doctor = commands.add_parser("doctor", help="Validate one consumer integration")
    _add_common(doctor)
    doctor.add_argument("--profile")
    doctor.set_defaults(handler=command_doctor)

    setup = commands.add_parser("setup", help=argparse.SUPPRESS)
    _add_common(setup)
    setup.add_argument("--profile", required=True)
    setup.add_argument("--apply", action="store_true")
    setup.add_argument("--allow", action="append", default=[])
    setup.set_defaults(handler=command_setup)

    verify = commands.add_parser("verify", help="Run a project verification profile")
    _add_common(verify)
    verify.add_argument("--profile", required=True)
    verify.set_defaults(handler=command_verify)

    adoption = commands.add_parser("adoption", help="Apply or check managed adoption")
    adoption_commands = adoption.add_subparsers(dest="adoption_command", required=True)
    for name, handler in (("apply", command_adoption_apply), ("check", command_adoption_check)):
        adoption_command = adoption_commands.add_parser(name)
        _add_common(adoption_command)
        adoption_command.add_argument("--requirements-lock", type=Path, required=True)
        if name == "apply":
            adoption_command.add_argument("--requirements-source", type=Path)
            adoption_command.add_argument("--expected-requirements-digest")
        adoption_command.set_defaults(handler=handler)

    change = commands.add_parser("change", help="Run the six-phase change lifecycle")
    change_commands = change.add_subparsers(dest="change_command", required=True)

    change_start = change_commands.add_parser("start")
    _add_common(change_start)
    _add_actor(change_start)
    change_start.add_argument("--contract", type=Path, required=True)
    change_start.set_defaults(handler=command_change_start)

    change_plan = change_commands.add_parser("plan")
    _add_common(change_plan)
    _add_actor(change_plan)
    change_plan.add_argument("--change-id", required=True)
    change_plan.add_argument("--plan", type=Path, required=True)
    change_plan.set_defaults(handler=command_change_plan)

    change_implement = change_commands.add_parser("implement")
    _add_common(change_implement)
    _add_actor(change_implement)
    change_implement.add_argument("--change-id", required=True)
    change_implement.set_defaults(handler=command_change_implement)

    change_verify = change_commands.add_parser("verify")
    _add_common(change_verify)
    change_verify.add_argument("--change-id", required=True)
    change_verify.add_argument("--profile", required=True)
    change_verify.set_defaults(handler=command_change_verify)

    review = change_commands.add_parser("review")
    review_commands = review.add_subparsers(dest="review_command", required=True)
    review_start = review_commands.add_parser("start")
    _add_common(review_start)
    _add_actor(review_start)
    review_start.add_argument("--change-id", required=True)
    review_start.set_defaults(handler=command_change_review_start)
    review_submit = review_commands.add_parser("submit")
    _add_common(review_submit)
    review_submit.add_argument("--change-id", required=True)
    review_submit.add_argument("--review", type=Path, required=True)
    review_submit.set_defaults(handler=command_change_review_submit)

    change_finish = change_commands.add_parser("finish")
    _add_common(change_finish)
    _add_actor(change_finish)
    change_finish.add_argument("--change-id", required=True)
    change_finish.set_defaults(handler=command_change_finish)

    change_status = change_commands.add_parser("status")
    _add_common(change_status)
    change_status.add_argument("--change-id", required=True)
    change_status.set_defaults(handler=command_change_status)

    release = commands.add_parser("release", help="Validate release identity")
    release_commands = release.add_subparsers(dest="release_command", required=True)
    release_validate = release_commands.add_parser("validate")
    _add_common(release_validate)
    release_validate.add_argument("--tag")
    release_validate.set_defaults(handler=command_release_validate)

    publication = commands.add_parser("publication", help=argparse.SUPPRESS)
    publication_commands = publication.add_subparsers(
        dest="publication_command", required=True
    )
    publication_branch = publication_commands.add_parser("validate-branch")
    publication_branch.add_argument("--branch", required=True)
    _add_common(publication_branch, project=False)
    publication_branch.set_defaults(handler=command_publication_branch)
    publication_commit = publication_commands.add_parser("validate-commit")
    publication_commit.add_argument("--subject", required=True)
    _add_common(publication_commit, project=False)
    publication_commit.set_defaults(handler=command_publication_commit)
    publication_range = publication_commands.add_parser("validate-range")
    _add_common(publication_range)
    publication_range.add_argument("--branch", required=True)
    publication_range.add_argument("--range", dest="range_spec", required=True)
    publication_range.set_defaults(handler=command_publication_range)
    publication_pr = publication_commands.add_parser("validate-pr")
    publication_pr.add_argument("--title", required=True)
    publication_pr.add_argument("--branch", required=True)
    publication_pr.add_argument("--state", required=True)
    publication_pr.add_argument("--body-file", type=Path)
    _add_common(publication_pr, project=False)
    publication_pr.set_defaults(handler=command_publication_pr)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(arguments)
    try:
        value, code = args.handler(args)
    except (ProcessError, OSError, ValueError) as error:
        value = _result(args.command, status="failed", errors=str(error).splitlines())
        _emit(value, as_json="--json" in arguments)
        return 2
    _emit(value, as_json=args.json)
    return code
