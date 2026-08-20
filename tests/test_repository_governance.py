import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jsonschema

import engineering_process.repository_governance as governance_module
from engineering_process.contracts import (
    ContractError,
    validate_repository_governance,
)
from engineering_process.repository_governance import (
    GITHUB_ACTIONS_INTEGRATION_ID,
    GITHUB_TIMEOUT_SECONDS,
    MAX_GITHUB_RESPONSE_BYTES,
    GitHubApi,
    apply_github_ruleset_plan,
    check_github_repository,
    desired_ruleset,
    initialize_policy,
    plan_github_ruleset,
    ruleset_issues,
    write_plan,
)


REPOSITORY = "example-owner/sample-project"
PROCESS_ROOT = Path(__file__).resolve().parent.parent
RULESETS_PATH = (
    f"/repos/{REPOSITORY}/rulesets"
    "?includes_parents=false&targets=branch&per_page=100"
)
RULESET_PATH = f"/repos/{REPOSITORY}/rulesets/42?includes_parents=false"
PULL_PATH = f"/repos/{REPOSITORY}/pulls/7"
HEAD_SHA = "a" * 40
CHECKS_PATH = (
    f"/repos/{REPOSITORY}/commits/{HEAD_SHA}/check-runs"
    f"?filter=latest&per_page=100&app_id={GITHUB_ACTIONS_INTEGRATION_ID}"
)


class FakeApi:
    def __init__(self, responses):
        self.responses = copy.deepcopy(responses)
        self.writes = []

    def get(self, path):
        if path not in self.responses:
            raise AssertionError(f"unexpected GET {path}")
        return copy.deepcopy(self.responses[path])

    def write(self, method, path, document):
        self.writes.append((method, path, copy.deepcopy(document)))
        identifier = 42
        if method == "PUT":
            identifier = int(path.rsplit("/", 1)[1])
        applied = {
            "id": identifier,
            "source_type": "Repository",
            **copy.deepcopy(document),
        }
        self.responses[
            f"/repos/{REPOSITORY}/rulesets/{identifier}?includes_parents=false"
        ] = applied
        return copy.deepcopy(applied)


class DriftAfterWriteApi(FakeApi):
    def write(self, method, path, document):
        response = super().write(method, path, document)
        identifier = response["id"]
        detail_path = (
            f"/repos/{REPOSITORY}/rulesets/{identifier}?includes_parents=false"
        )
        self.responses[detail_path]["bypass_actors"] = [
            {"actor_id": 1, "actor_type": "User", "bypass_mode": "always"}
        ]
        return response


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, limit):
        return self.content[:limit]


class FakeOpener:
    def __init__(self, content):
        self.content = content
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return FakeResponse(self.content)


def policy_document():
    return {
        "schemaVersion": 1,
        "defaultBranch": {
            "requireChangeRequest": True,
            "blockDeletion": True,
            "blockNonFastForward": True,
            "bypass": "forbidden",
            "requireUpToDate": False,
            "requiredChecks": ["Change metadata policy", "Merge eligibility"],
        },
    }


def per_job_ruleset():
    return {
        "id": 42,
        "name": "Protect main branch",
        "target": "branch",
        "source_type": "Repository",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {
            "ref_name": {"exclude": [], "include": ["~DEFAULT_BRANCH"]}
        },
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
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
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    "do_not_enforce_on_create": False,
                    "required_status_checks": [
                        {
                            "context": "Project tests",
                            "integration_id": GITHUB_ACTIONS_INTEGRATION_ID,
                        }
                    ],
                },
            },
        ],
    }


def successful_responses(ruleset=None):
    summaries = []
    responses = {
        f"/repos/{REPOSITORY}": {
            "full_name": REPOSITORY,
            "default_branch": "main",
        },
        RULESETS_PATH: summaries,
        PULL_PATH: {
            "number": 7,
            "base": {"ref": "main"},
            "head": {"sha": HEAD_SHA},
        },
        CHECKS_PATH: {
            "total_count": 2,
            "check_runs": [
                {
                    "id": 101,
                    "name": "Change metadata policy",
                    "status": "completed",
                    "conclusion": "success",
                    "app": {"id": GITHUB_ACTIONS_INTEGRATION_ID},
                },
                {
                    "id": 102,
                    "name": "Merge eligibility",
                    "status": "completed",
                    "conclusion": "success",
                    "app": {"id": GITHUB_ACTIONS_INTEGRATION_ID},
                },
            ],
        },
    }
    if ruleset is not None:
        summaries.append(
            {
                "id": ruleset["id"],
                "target": "branch",
                "source_type": "Repository",
            }
        )
        responses[RULESET_PATH] = ruleset
    return responses


class RepositoryGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.document = policy_document()
        self.policy = validate_repository_governance(self.document)

    def test_per_job_required_check_list_is_reported_as_drift(self):
        current = per_job_ruleset()
        api = FakeApi(successful_responses(current))

        result = check_github_repository(api, REPOSITORY, self.policy)

        self.assertEqual(["required-status-checks-mismatch"], result["issues"])
        self.assertEqual(42, result["rulesetId"])

    def test_desired_ruleset_preserves_stronger_project_rules(self):
        current = per_job_ruleset()
        current["rules"].append(
            {"type": "required_signatures"}
        )
        desired = desired_ruleset(self.policy, current)

        self.assertEqual([], desired["bypass_actors"])
        self.assertIn({"type": "required_signatures"}, desired["rules"])
        status_rule = next(
            rule for rule in desired["rules"] if rule["type"] == "required_status_checks"
        )
        self.assertEqual(
            ["Change metadata policy", "Merge eligibility"],
            [
                item["context"]
                for item in status_rule["parameters"]["required_status_checks"]
            ],
        )

    def test_plan_binds_current_state_and_successful_check_evidence(self):
        current = per_job_ruleset()
        api = FakeApi(successful_responses(current))

        plan = plan_github_ruleset(
            api,
            REPOSITORY,
            self.document,
            self.policy,
            evidence_pull_request=7,
        )

        self.assertEqual("update", plan["ruleset"]["action"])
        self.assertEqual(42, plan["ruleset"]["id"])
        self.assertEqual(HEAD_SHA, plan["evidence"]["headSha"])
        self.assertEqual(
            ["Change metadata policy", "Merge eligibility"],
            [item["name"] for item in plan["evidence"]["checks"]],
        )
        schema = json.loads(
            (
                PROCESS_ROOT
                / "schemas"
                / "repository-governance-plan.schema.json"
            ).read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator(schema).validate(plan)

    def test_plan_rejects_missing_or_failed_required_checks(self):
        responses = successful_responses(per_job_ruleset())
        responses[CHECKS_PATH]["check_runs"].pop()
        responses[CHECKS_PATH]["total_count"] = 1
        with self.assertRaisesRegex(
            ContractError, "were not observed.*Merge eligibility"
        ):
            plan_github_ruleset(
                FakeApi(responses),
                REPOSITORY,
                self.document,
                self.policy,
                evidence_pull_request=7,
            )

        responses = successful_responses(per_job_ruleset())
        responses[CHECKS_PATH]["check_runs"][0]["conclusion"] = "failure"
        with self.assertRaisesRegex(
            ContractError, "not successful.*Change metadata policy"
        ):
            plan_github_ruleset(
                FakeApi(responses),
                REPOSITORY,
                self.document,
                self.policy,
                evidence_pull_request=7,
            )

    def test_apply_rechecks_state_evidence_and_exact_repository(self):
        current = per_job_ruleset()
        api = FakeApi(successful_responses(current))
        plan = plan_github_ruleset(
            api,
            REPOSITORY,
            self.document,
            self.policy,
            evidence_pull_request=7,
        )

        result = apply_github_ruleset_plan(
            api,
            plan,
            self.document,
            self.policy,
            confirm_repository=REPOSITORY,
        )

        self.assertTrue(result["mutated"])
        self.assertEqual("update", result["action"])
        self.assertEqual(
            ("PUT", f"/repos/{REPOSITORY}/rulesets/42"),
            api.writes[0][:2],
        )

        with self.assertRaisesRegex(ContractError, "does not match"):
            apply_github_ruleset_plan(
                api,
                plan,
                self.document,
                self.policy,
                confirm_repository="example-owner/different-project",
            )

        stale_api = FakeApi(successful_responses(current))
        stale_api.responses[RULESET_PATH]["enforcement"] = "disabled"
        with self.assertRaisesRegex(ContractError, "changed after planning"):
            apply_github_ruleset_plan(
                stale_api,
                plan,
                self.document,
                self.policy,
                confirm_repository=REPOSITORY,
            )

    def test_create_plan_refuses_new_competing_ruleset_before_apply(self):
        api = FakeApi(successful_responses())
        plan = plan_github_ruleset(
            api,
            REPOSITORY,
            self.document,
            self.policy,
            evidence_pull_request=7,
        )
        self.assertEqual("create", plan["ruleset"]["action"])

        api.responses.update(successful_responses(per_job_ruleset()))
        with self.assertRaisesRegex(ContractError, "appeared after planning"):
            apply_github_ruleset_plan(
                api,
                plan,
                self.document,
                self.policy,
                confirm_repository=REPOSITORY,
            )

    def test_apply_fails_if_read_back_does_not_match_the_plan(self):
        api = DriftAfterWriteApi(successful_responses(per_job_ruleset()))
        plan = plan_github_ruleset(
            api,
            REPOSITORY,
            self.document,
            self.policy,
            evidence_pull_request=7,
        )

        with self.assertRaisesRegex(ContractError, "does not match"):
            apply_github_ruleset_plan(
                api,
                plan,
                self.document,
                self.policy,
                confirm_repository=REPOSITORY,
            )

    def test_multiple_default_branch_rulesets_are_ambiguous(self):
        responses = successful_responses(per_job_ruleset())
        second = copy.deepcopy(per_job_ruleset())
        second["id"] = 43
        second["name"] = "Another default rule"
        responses[RULESETS_PATH].append(
            {"id": 43, "target": "branch", "source_type": "Repository"}
        )
        responses[
            f"/repos/{REPOSITORY}/rulesets/43?includes_parents=false"
        ] = second

        with self.assertRaisesRegex(ContractError, "ambiguous"):
            check_github_repository(FakeApi(responses), REPOSITORY, self.policy)

    def test_plan_does_not_weaken_a_ruleset_covering_additional_refs(self):
        current = per_job_ruleset()
        current["conditions"]["ref_name"]["include"].append("refs/heads/release/*")

        with self.assertRaisesRegex(ContractError, "targets refs beyond"):
            plan_github_ruleset(
                FakeApi(successful_responses(current)),
                REPOSITORY,
                self.document,
                self.policy,
                evidence_pull_request=7,
            )

    def test_policy_init_and_plan_writes_are_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            details = initialize_policy(root)
            self.assertEqual(
                ".process/repository-governance.json", details["path"]
            )
            validate_repository_governance(
                json.loads(
                    (root / details["path"]).read_text(encoding="utf-8")
                )
            )
            with self.assertRaisesRegex(ContractError, "already exists"):
                initialize_policy(root)
            self.assertEqual(
                [], list((root / ".process").glob(".repository-governance.json.*.tmp"))
            )

            api = FakeApi(successful_responses(per_job_ruleset()))
            plan = plan_github_ruleset(
                api,
                REPOSITORY,
                self.document,
                self.policy,
                evidence_pull_request=7,
            )
            output = root / "plan.json"
            write_plan(output, plan)
            with self.assertRaisesRegex(ContractError, "already exists"):
                write_plan(output, plan)
            self.assertEqual([], list(root.glob(".plan.json.*.tmp")))

    @unittest.skipIf(os.name == "nt", "Windows symlink creation needs host policy")
    def test_policy_init_rejects_a_symlinked_process_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            target = base / "outside"
            root.mkdir()
            target.mkdir()
            (root / ".process").symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(ContractError, "regular directory"):
                initialize_policy(root)

    def test_unverifiable_bypass_state_fails_closed(self):
        current = per_job_ruleset()
        del current["bypass_actors"]
        with self.assertRaisesRegex(ContractError, "bypass_actors"):
            ruleset_issues(self.policy, current)

    def test_github_api_requires_environment_credentials_and_bounds_output(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(
            ContractError, "GH_TOKEN or GITHUB_TOKEN"
        ):
            GitHubApi.from_environment()

        api = GitHubApi("secret-value")
        opener = FakeOpener(b"{}")
        api._opener = opener
        self.assertEqual({}, api.get("/repos/example-owner/sample-project"))
        request, timeout = opener.requests[0]
        self.assertEqual(GITHUB_TIMEOUT_SECONDS, timeout)
        self.assertEqual("Bearer secret-value", request.get_header("Authorization"))

        api._opener = FakeOpener(b"x" * (MAX_GITHUB_RESPONSE_BYTES + 1))
        with self.assertRaisesRegex(ContractError, "response exceeds"):
            api.get("/repos/example-owner/sample-project")

    def test_github_api_does_not_forward_credentials_to_redirected_hosts(self):
        handler = governance_module._HttpsOnlyRedirectHandler()
        request = governance_module.urllib.request.Request(
            "https://api.github.com/repos/example-owner/sample-project"
        )
        for target in (
            "http://api.github.com/repos/example-owner/sample-project",
            "https://example.invalid/repos/example-owner/sample-project",
            "https://api.github.com@example.invalid/repository",
        ):
            with self.subTest(target=target), self.assertRaisesRegex(
                ContractError, "untrusted redirect"
            ):
                handler.redirect_request(request, None, 302, "Found", {}, target)


if __name__ == "__main__":
    unittest.main()
