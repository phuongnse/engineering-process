from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .contracts import (
    MAX_JSON_BYTES,
    ContractError,
    RepositoryGovernance,
    read_json,
    validate_repository_governance,
)


GITHUB_API_ROOT = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
GITHUB_ACTIONS_INTEGRATION_ID = 15368
GITHUB_TIMEOUT_SECONDS = 30
MAX_GITHUB_RESPONSE_BYTES = 1_000_000
MAX_GITHUB_RULESETS = 100
MAX_DEFAULT_BRANCH_RULESETS = 16
MANAGED_RULESET_NAME = "Engineering process default branch"
REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?$"
)
GIT_OID_PATTERN = re.compile(r"^[0-9a-f]{40}$")
BASELINE_RULES = {
    "deletion",
    "non_fast_forward",
    "pull_request",
    "required_status_checks",
}
DEFAULT_POLICY = {
    "schemaVersion": 1,
    "defaultBranch": {
        "requireChangeRequest": True,
        "blockDeletion": True,
        "blockHistoryRewrite": True,
        "bypass": "forbidden",
        "requireUpToDate": False,
        "requiredChecks": ["Change metadata policy", "Merge eligibility"],
    },
}


class RepositoryApi(Protocol):
    def get(self, path: str) -> Any: ...

    def write(self, method: str, path: str, document: dict[str, Any]) -> Any: ...


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        try:
            port = target.port
        except ValueError as error:
            raise ContractError("GitHub API refused an invalid redirect") from error
        if (
            target.scheme.lower() != "https"
            or target.hostname != "api.github.com"
            or port not in {None, 443}
            or target.username is not None
            or target.password is not None
            or target.fragment
        ):
            raise ContractError("GitHub API refused an untrusted redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHubApi:
    def __init__(self, token: str) -> None:
        if (
            not token
            or token != token.strip()
            or len(token) > 4096
            or any(character.isspace() for character in token)
        ):
            raise ContractError(
                "GitHub token must be a non-empty bounded environment value"
            )
        self._token = token
        self._opener = urllib.request.build_opener(_HttpsOnlyRedirectHandler())

    @classmethod
    def from_environment(cls) -> GitHubApi:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token is None:
            raise ContractError(
                "GitHub repository governance requires GH_TOKEN or GITHUB_TOKEN "
                "in the environment"
            )
        return cls(token)

    def _request(
        self,
        method: str,
        path: str,
        document: dict[str, Any] | None = None,
    ) -> Any:
        if not path.startswith("/") or "//" in path or ".." in path:
            raise ContractError("GitHub API path is invalid")
        content = None
        if document is not None:
            content = _canonical_bytes(document)
            if len(content) > MAX_JSON_BYTES:
                raise ContractError("GitHub API request exceeds the 1 MB limit")
        request = urllib.request.Request(
            GITHUB_API_ROOT + path,
            data=content,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "engineering-process/repository-governance",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
        )
        try:
            with self._opener.open(
                request, timeout=GITHUB_TIMEOUT_SECONDS
            ) as response:
                raw = response.read(MAX_GITHUB_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raw = error.read(4097)
            detail = raw[:4096].decode("utf-8", errors="replace").strip()
            raise ContractError(
                f"GitHub API {method} {path} failed with HTTP {error.code}"
                + (f": {detail}" if detail else "")
            ) from error
        except (OSError, ValueError) as error:
            raise ContractError(
                f"GitHub API {method} {path} failed: {error}"
            ) from error
        if len(raw) > MAX_GITHUB_RESPONSE_BYTES:
            raise ContractError(
                f"GitHub API {method} {path} response exceeds "
                f"{MAX_GITHUB_RESPONSE_BYTES} bytes"
            )
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContractError(
                f"GitHub API {method} {path} returned invalid JSON"
            ) from error

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def write(self, method: str, path: str, document: dict[str, Any]) -> Any:
        if method not in {"POST", "PUT"}:
            raise ContractError("GitHub API write method must be POST or PUT")
        return self._request(method, path, document)


def _canonical_bytes(document: Any) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(document: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(document)).hexdigest()}"


def _repository(value: str) -> str:
    if (
        REPOSITORY_PATTERN.fullmatch(value) is None
        or ".." in value
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise ContractError("repository must use canonical OWNER/REPOSITORY syntax")
    return value


def _positive_integer(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractError(f"{path}: must be a positive integer")
    return value


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{path}: must be an object")
    return value


def _array(value: Any, path: str, *, maximum: int) -> list[Any]:
    if not isinstance(value, list):
        raise ContractError(f"{path}: must be an array")
    if len(value) > maximum:
        raise ContractError(f"{path}: exceeds {maximum} items")
    return value


def _string(value: Any, path: str, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or "\x00" in value
    ):
        raise ContractError(f"{path}: must be a non-empty bounded trimmed string")
    return value


def policy_path(project_root: Path) -> Path:
    return project_root / ".process" / "repository-governance.json"


def load_policy(project_root: Path) -> tuple[dict[str, Any], RepositoryGovernance]:
    path = policy_path(project_root)
    document = read_json(path)
    return document, validate_repository_governance(document, str(path))


def _write_exclusive(path: Path, content: bytes) -> None:
    if len(content) > MAX_JSON_BYTES:
        raise ContractError(f"{path}: content exceeds the 1 MB limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
    except OSError as error:
        raise ContractError(f"{path}: cannot create temporary file: {error}") from error
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as error:
            raise ContractError(f"{path}: already exists") from error
        except OSError as error:
            raise ContractError(f"{path}: cannot create: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise ContractError(
                f"{path}: cannot clean temporary file: {error}"
            ) from error


def initialize_policy(project_root: Path) -> dict[str, Any]:
    root = project_root.resolve()
    process_directory = root / ".process"
    try:
        process_directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise ContractError(f"{process_directory}: cannot create: {error}") from error
    try:
        process_directory.lstat()
    except OSError as error:
        raise ContractError(f"{process_directory}: cannot inspect: {error}") from error
    if process_directory.is_symlink() or not process_directory.is_dir():
        raise ContractError(f"{process_directory}: must be a regular directory")
    if process_directory.resolve() != process_directory:
        raise ContractError(f"{process_directory}: resolves outside the project")
    path = process_directory / "repository-governance.json"
    content = json.dumps(
        DEFAULT_POLICY, ensure_ascii=False, indent=2, sort_keys=False
    ).encode("utf-8") + b"\n"
    _write_exclusive(path, content)
    return {"path": path.relative_to(root).as_posix(), "policy": DEFAULT_POLICY}


def _repository_metadata(api: RepositoryApi, repository: str) -> tuple[str, str]:
    document = _object(api.get(f"/repos/{repository}"), "GitHub repository")
    full_name = _string(document.get("full_name"), "GitHub repository.full_name")
    if full_name.lower() != repository.lower():
        raise ContractError("GitHub API returned a different repository identity")
    default_branch = _string(
        document.get("default_branch"), "GitHub repository.default_branch"
    )
    return full_name, default_branch


def _ruleset_payload(document: Any, path: str = "GitHub ruleset") -> dict[str, Any]:
    value = _object(document, path)
    payload = {
        "name": _string(value.get("name"), f"{path}.name"),
        "target": value.get("target"),
        "enforcement": value.get("enforcement"),
        "bypass_actors": value.get("bypass_actors"),
        "conditions": value.get("conditions"),
        "rules": value.get("rules"),
    }
    if payload["target"] != "branch":
        raise ContractError(f"{path}.target: must be branch")
    if payload["enforcement"] not in {"active", "disabled", "evaluate"}:
        raise ContractError(f"{path}.enforcement: invalid value")
    _array(payload["bypass_actors"], f"{path}.bypass_actors", maximum=64)
    _object(payload["conditions"], f"{path}.conditions")
    rules = _array(payload["rules"], f"{path}.rules", maximum=64)
    for index, rule in enumerate(rules):
        item = _object(rule, f"{path}.rules[{index}]")
        _string(item.get("type"), f"{path}.rules[{index}].type", maximum=64)
    if len(_canonical_bytes(payload)) > MAX_JSON_BYTES:
        raise ContractError(f"{path}: exceeds the 1 MB limit")
    return payload


def _targets_default_branch(ruleset: dict[str, Any]) -> bool:
    conditions = ruleset.get("conditions")
    if not isinstance(conditions, dict):
        return False
    ref_name = conditions.get("ref_name")
    if not isinstance(ref_name, dict):
        return False
    include = ref_name.get("include")
    return isinstance(include, list) and "~DEFAULT_BRANCH" in include


def _default_branch_rulesets(
    api: RepositoryApi, repository: str
) -> list[dict[str, Any]]:
    summaries = _array(
        api.get(
            f"/repos/{repository}/rulesets?includes_parents=false&targets=branch&per_page=100"
        ),
        "GitHub rulesets",
        maximum=MAX_GITHUB_RULESETS,
    )
    if len(summaries) == MAX_GITHUB_RULESETS:
        raise ContractError(
            "GitHub ruleset inventory reached the 100-item pagination boundary"
        )
    identifiers: list[int] = []
    for index, summary in enumerate(summaries):
        item = _object(summary, f"GitHub rulesets[{index}]")
        if item.get("target") != "branch" or item.get("source_type") != "Repository":
            continue
        identifiers.append(
            _positive_integer(item.get("id"), f"GitHub rulesets[{index}].id")
        )
    if len(identifiers) > MAX_DEFAULT_BRANCH_RULESETS:
        raise ContractError(
            f"GitHub branch ruleset count exceeds {MAX_DEFAULT_BRANCH_RULESETS}"
        )
    matches: list[dict[str, Any]] = []
    for identifier in identifiers:
        detail = _object(
            api.get(
                f"/repos/{repository}/rulesets/{identifier}?includes_parents=false"
            ),
            f"GitHub ruleset {identifier}",
        )
        if detail.get("id") != identifier:
            raise ContractError(
                f"GitHub ruleset {identifier}: API returned a different identity"
            )
        if _targets_default_branch(detail):
            _ruleset_payload(detail, f"GitHub ruleset {identifier}")
            matches.append(detail)
    return matches


def _select_ruleset(rulesets: list[dict[str, Any]]) -> dict[str, Any] | None:
    managed = [item for item in rulesets if item.get("name") == MANAGED_RULESET_NAME]
    if len(managed) == 1:
        return managed[0]
    if len(managed) > 1:
        raise ContractError("multiple managed default-branch rulesets are ambiguous")
    if len(rulesets) == 1:
        return rulesets[0]
    if len(rulesets) > 1:
        raise ContractError(
            "multiple existing default-branch rulesets are ambiguous; consolidate "
            "them before process management"
        )
    return None


def _rules_by_type(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rules: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(payload["rules"]):
        rule = _object(raw, f"GitHub ruleset.rules[{index}]")
        rule_type = _string(rule.get("type"), f"GitHub ruleset.rules[{index}].type")
        if rule_type in rules:
            raise ContractError(f"GitHub ruleset has duplicate {rule_type} rules")
        rules[rule_type] = rule
    return rules


def desired_ruleset(
    policy: RepositoryGovernance,
    current: dict[str, Any] | None,
) -> dict[str, Any]:
    current_payload = _ruleset_payload(current) if current is not None else None
    existing_rules = _rules_by_type(current_payload) if current_payload else {}
    pull_request = existing_rules.get("pull_request")
    if pull_request is None:
        pull_request = {
            "type": "pull_request",
            "parameters": {
                "required_approving_review_count": 0,
                "dismiss_stale_reviews_on_push": False,
                "required_reviewers": [],
                "require_code_owner_review": False,
                "require_last_push_approval": False,
                "required_review_thread_resolution": False,
                "allowed_merge_methods": ["merge", "squash", "rebase"],
            },
        }
    preserved = [
        rule
        for rule_type, rule in existing_rules.items()
        if rule_type not in BASELINE_RULES
    ]
    preserved.sort(key=lambda item: (str(item.get("type")), _canonical_bytes(item)))
    return {
        "name": current_payload["name"] if current_payload else MANAGED_RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {
            "ref_name": {"exclude": [], "include": ["~DEFAULT_BRANCH"]}
        },
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            pull_request,
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": policy.require_up_to_date,
                    "do_not_enforce_on_create": False,
                    "required_status_checks": [
                        {
                            "context": context,
                            "integration_id": GITHUB_ACTIONS_INTEGRATION_ID,
                        }
                        for context in policy.required_checks
                    ],
                },
            },
            *preserved,
        ],
    }


def ruleset_issues(
    policy: RepositoryGovernance,
    current: dict[str, Any] | None,
) -> list[str]:
    if current is None:
        return ["default-branch-ruleset-missing"]
    payload = _ruleset_payload(current)
    issues: list[str] = []
    if payload["enforcement"] != "active":
        issues.append("ruleset-not-active")
    if payload["bypass_actors"] != []:
        issues.append("bypass-actors-present-or-unverifiable")
    expected_conditions = {
        "ref_name": {"exclude": [], "include": ["~DEFAULT_BRANCH"]}
    }
    if payload["conditions"] != expected_conditions:
        issues.append("default-branch-condition-mismatch")
    rules = _rules_by_type(payload)
    for rule_type in ("deletion", "non_fast_forward", "pull_request"):
        if rule_type not in rules:
            issues.append(f"{rule_type.replace('_', '-')}-rule-missing")
    status_rule = rules.get("required_status_checks")
    if status_rule is None:
        issues.append("required-status-checks-rule-missing")
    else:
        expected = desired_ruleset(policy, current)["rules"][3]
        if status_rule != expected:
            issues.append("required-status-checks-mismatch")
    return issues


def check_github_repository(
    api: RepositoryApi,
    repository: str,
    policy: RepositoryGovernance,
) -> dict[str, Any]:
    repository = _repository(repository)
    full_name, default_branch = _repository_metadata(api, repository)
    current = _select_ruleset(_default_branch_rulesets(api, repository))
    desired = desired_ruleset(policy, current)
    return {
        "repository": full_name,
        "defaultBranch": default_branch,
        "rulesetId": current.get("id") if current else None,
        "rulesetName": current.get("name") if current else None,
        "currentDigest": _digest(_ruleset_payload(current)) if current else None,
        "desiredDigest": _digest(desired),
        "requiredChecks": list(policy.required_checks),
        "issues": ruleset_issues(policy, current),
    }


def _successful_check_evidence(
    api: RepositoryApi,
    repository: str,
    pull_request: int,
    default_branch: str,
    required_checks: tuple[str, ...],
) -> dict[str, Any]:
    pull_request = _positive_integer(pull_request, "evidence pull request")
    pull = _object(
        api.get(f"/repos/{repository}/pulls/{pull_request}"),
        "GitHub pull request",
    )
    if pull.get("number") != pull_request:
        raise ContractError("GitHub API returned a different pull-request number")
    base = _object(pull.get("base"), "GitHub pull request.base")
    if base.get("ref") != default_branch:
        raise ContractError(
            f"pull request {pull_request} does not target default branch {default_branch}"
        )
    head = _object(pull.get("head"), "GitHub pull request.head")
    head_sha = _string(head.get("sha"), "GitHub pull request.head.sha", maximum=64)
    if GIT_OID_PATTERN.fullmatch(head_sha) is None:
        raise ContractError("GitHub pull-request head must be a lowercase Git SHA-1")
    checks_document = _object(
        api.get(
            f"/repos/{repository}/commits/{head_sha}/check-runs"
            f"?filter=latest&per_page=100&app_id={GITHUB_ACTIONS_INTEGRATION_ID}"
        ),
        "GitHub check runs",
    )
    total = checks_document.get("total_count")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ContractError("GitHub check runs.total_count is invalid")
    runs = _array(
        checks_document.get("check_runs"),
        "GitHub check runs.check_runs",
        maximum=100,
    )
    if total > len(runs):
        raise ContractError("GitHub check-run evidence exceeds one bounded page")
    latest: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(runs):
        run = _object(raw, f"GitHub check runs.check_runs[{index}]")
        name = _string(
            run.get("name"), f"GitHub check runs.check_runs[{index}].name"
        )
        if name not in required_checks:
            continue
        app = _object(run.get("app"), f"GitHub check runs.check_runs[{index}].app")
        if app.get("id") != GITHUB_ACTIONS_INTEGRATION_ID:
            continue
        identifier = _positive_integer(
            run.get("id"), f"GitHub check runs.check_runs[{index}].id"
        )
        previous = latest.get(name)
        if previous is None or identifier > previous["id"]:
            latest[name] = {
                "id": identifier,
                "name": name,
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
            }
    missing = sorted(set(required_checks) - set(latest))
    if missing:
        raise ContractError(
            "required checks were not observed on the evidence checkpoint: "
            + ", ".join(missing)
        )
    unsuccessful = sorted(
        name
        for name, run in latest.items()
        if run["status"] != "completed" or run["conclusion"] != "success"
    )
    if unsuccessful:
        raise ContractError(
            "required checks are not successful on the evidence checkpoint: "
            + ", ".join(unsuccessful)
        )
    return {
        "pullRequest": pull_request,
        "headSha": head_sha,
        "checks": [latest[name] for name in sorted(latest)],
    }


def plan_github_ruleset(
    api: RepositoryApi,
    repository: str,
    policy_document: dict[str, Any],
    policy: RepositoryGovernance,
    *,
    evidence_pull_request: int,
) -> dict[str, Any]:
    repository = _repository(repository)
    full_name, default_branch = _repository_metadata(api, repository)
    current = _select_ruleset(_default_branch_rulesets(api, repository))
    if current is not None:
        current_conditions = _ruleset_payload(current)["conditions"]
        expected_conditions = {
            "ref_name": {"exclude": [], "include": ["~DEFAULT_BRANCH"]}
        }
        if current_conditions != expected_conditions:
            raise ContractError(
                "existing ruleset targets refs beyond the exact default-branch "
                "policy; split or correct it manually before process management"
            )
    desired = desired_ruleset(policy, current)
    current_payload = _ruleset_payload(current) if current else None
    current_digest = _digest(current_payload) if current_payload else None
    desired_digest = _digest(desired)
    evidence = _successful_check_evidence(
        api,
        repository,
        evidence_pull_request,
        default_branch,
        policy.required_checks,
    )
    action = (
        "create"
        if current is None
        else "none"
        if current_digest == desired_digest
        else "update"
    )
    return {
        "schemaVersion": 1,
        "provider": "github",
        "repository": full_name,
        "policyDigest": _digest(policy_document),
        "ruleset": {
            "action": action,
            "id": current.get("id") if current else None,
            "currentDigest": current_digest,
            "desiredDigest": desired_digest,
            "payload": desired,
        },
        "evidence": evidence,
    }


def validate_repository_governance_plan(
    document: Any, path: str = "repository-governance plan"
) -> dict[str, Any]:
    value = _object(document, path)
    expected = {
        "schemaVersion",
        "provider",
        "repository",
        "policyDigest",
        "ruleset",
        "evidence",
    }
    if set(value) != expected:
        raise ContractError(f"{path}: has invalid plan properties")
    if value["schemaVersion"] != 1 or value["provider"] != "github":
        raise ContractError(f"{path}: unsupported repository-governance plan")
    _repository(value["repository"])
    if not isinstance(value["policyDigest"], str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", value["policyDigest"]
    ):
        raise ContractError(f"{path}.policyDigest: invalid digest")
    ruleset = _object(value["ruleset"], f"{path}.ruleset")
    if set(ruleset) != {
        "action",
        "id",
        "currentDigest",
        "desiredDigest",
        "payload",
    }:
        raise ContractError(f"{path}.ruleset: has invalid properties")
    if ruleset["action"] not in {"create", "update", "none"}:
        raise ContractError(f"{path}.ruleset.action: invalid action")
    if ruleset["action"] == "create":
        if ruleset["id"] is not None or ruleset["currentDigest"] is not None:
            raise ContractError(f"{path}.ruleset: create plan has current state")
    else:
        _positive_integer(ruleset["id"], f"{path}.ruleset.id")
        if not isinstance(ruleset["currentDigest"], str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", ruleset["currentDigest"]
        ):
            raise ContractError(f"{path}.ruleset.currentDigest: invalid digest")
    payload = _ruleset_payload(ruleset["payload"], f"{path}.ruleset.payload")
    if _digest(payload) != ruleset["desiredDigest"]:
        raise ContractError(f"{path}.ruleset.desiredDigest: does not match payload")
    evidence = _object(value["evidence"], f"{path}.evidence")
    if set(evidence) != {"pullRequest", "headSha", "checks"}:
        raise ContractError(f"{path}.evidence: has invalid properties")
    _positive_integer(evidence["pullRequest"], f"{path}.evidence.pullRequest")
    if not isinstance(evidence["headSha"], str) or GIT_OID_PATTERN.fullmatch(
        evidence["headSha"]
    ) is None:
        raise ContractError(f"{path}.evidence.headSha: invalid Git SHA-1")
    raw_checks = _array(
        evidence["checks"], f"{path}.evidence.checks", maximum=64
    )
    if len(raw_checks) < 2:
        raise ContractError(f"{path}.evidence.checks: must contain at least 2 items")
    check_names: list[str] = []
    for index, raw_check in enumerate(raw_checks):
        check_path = f"{path}.evidence.checks[{index}]"
        check = _object(raw_check, check_path)
        if set(check) != {"id", "name", "status", "conclusion"}:
            raise ContractError(f"{check_path}: has invalid properties")
        _positive_integer(check["id"], f"{check_path}.id")
        name = _string(check["name"], f"{check_path}.name", maximum=100)
        if check["status"] != "completed" or check["conclusion"] != "success":
            raise ContractError(f"{check_path}: must contain successful evidence")
        check_names.append(name)
    if check_names != sorted(check_names) or len(set(check_names)) != len(check_names):
        raise ContractError(
            f"{path}.evidence.checks: names must be unique and sorted"
        )
    return value


def write_plan(path: Path, document: dict[str, Any]) -> None:
    validate_repository_governance_plan(document, str(path))
    content = json.dumps(
        document, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8") + b"\n"
    if len(content) > MAX_JSON_BYTES:
        raise ContractError(f"{path}: plan exceeds the 1 MB limit")
    _write_exclusive(path, content)


def apply_github_ruleset_plan(
    api: RepositoryApi,
    plan_document: Any,
    policy_document: dict[str, Any],
    policy: RepositoryGovernance,
    *,
    confirm_repository: str,
) -> dict[str, Any]:
    plan = validate_repository_governance_plan(plan_document)
    repository = _repository(confirm_repository)
    if plan["repository"].lower() != repository.lower():
        raise ContractError(
            "--confirm-repository does not match the planned repository"
        )
    if plan["policyDigest"] != _digest(policy_document):
        raise ContractError("repository-governance policy changed after planning")
    full_name, default_branch = _repository_metadata(api, repository)
    if full_name.lower() != plan["repository"].lower():
        raise ContractError("GitHub repository identity changed after planning")
    current = _select_ruleset(_default_branch_rulesets(api, repository))
    planned_ruleset = plan["ruleset"]
    if planned_ruleset["action"] == "create":
        if current is not None:
            raise ContractError("default-branch ruleset appeared after planning")
        current_digest = None
    else:
        if current is None or current.get("id") != planned_ruleset["id"]:
            raise ContractError("planned default-branch ruleset identity is stale")
        current_digest = _digest(_ruleset_payload(current))
    if current_digest != planned_ruleset["currentDigest"]:
        raise ContractError("default-branch ruleset changed after planning")
    desired = desired_ruleset(policy, current)
    if (
        _digest(desired) != planned_ruleset["desiredDigest"]
        or desired != planned_ruleset["payload"]
    ):
        raise ContractError("desired default-branch ruleset changed after planning")
    evidence = _successful_check_evidence(
        api,
        repository,
        plan["evidence"]["pullRequest"],
        default_branch,
        policy.required_checks,
    )
    if evidence != plan["evidence"]:
        raise ContractError("required-check evidence changed after planning")
    action = planned_ruleset["action"]
    if action == "none":
        return {
            "repository": full_name,
            "action": "none",
            "rulesetId": current.get("id") if current else None,
            "rulesetDigest": planned_ruleset["desiredDigest"],
            "mutated": False,
        }
    if action == "create":
        result = api.write("POST", f"/repos/{repository}/rulesets", desired)
    else:
        result = api.write(
            "PUT",
            f"/repos/{repository}/rulesets/{planned_ruleset['id']}",
            desired,
        )
    response = _object(result, "GitHub applied ruleset response")
    applied_id = _positive_integer(
        response.get("id"), "GitHub applied ruleset response.id"
    )
    applied = _object(
        api.get(
            f"/repos/{repository}/rulesets/{applied_id}?includes_parents=false"
        ),
        "GitHub applied ruleset",
    )
    if applied.get("id") != applied_id:
        raise ContractError("GitHub ruleset read-back returned a different identity")
    applied_payload = _ruleset_payload(applied, "GitHub applied ruleset")
    issues = ruleset_issues(policy, applied)
    if issues or _digest(applied_payload) != planned_ruleset["desiredDigest"]:
        raise ContractError(
            "GitHub applied ruleset does not match the authorized plan"
            + (": " + ", ".join(issues) if issues else "")
        )
    return {
        "repository": full_name,
        "action": action,
        "rulesetId": applied_id,
        "rulesetDigest": _digest(applied_payload),
        "mutated": True,
    }
