from __future__ import annotations

from pathlib import Path
from typing import Any

from .bundles import load_bundles
from .command_catalog import LIFECYCLE_COMMAND_PATHS
from .contracts import ContractError, SKILL_PATTERN, read_json
from .lifecycle import PHASES
from .skills import skill_directories


MAX_GRAPH_STATES = 64
MAX_GRAPH_COMMANDS = 32
MAX_GRAPH_TRANSITIONS = 32
EXTERNAL_STATES = {"unregistered", "awaiting-human-merge"}


def process_graph_path(process_root: Path) -> Path:
    candidates = (
        process_root / "process-graph.json",
        process_root / "share" / "engineering-process" / "process-graph.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ContractError(f"{process_root}: cannot locate process-graph.json")


def process_root_from_skills(skills_root: Path) -> Path:
    for candidate in (skills_root.parent, skills_root.parent.parent):
        if (candidate / "process-graph.json").is_file():
            return candidate
    raise ContractError(f"{skills_root}: cannot locate owning process graph")


def _object(value: Any, label: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ContractError(f"{label}: must contain exactly {', '.join(sorted(keys))}")
    return value


def load_process_graph(
    process_root: Path,
    skills_root: Path,
    selected_skills: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    path = process_graph_path(process_root)
    document = _object(
        read_json(path),
        str(path),
        {"schemaVersion", "entrySkill", "states"},
    )
    if document["schemaVersion"] != 1:
        raise ContractError(f"{path}.schemaVersion: must be 1")
    available_skills = {directory.name for directory in skill_directories(skills_root)}
    bundles = load_bundles(process_root, skills_root)
    core_skills = set(bundles["core"])
    if selected_skills is not None:
        missing_selected = sorted(core_skills - set(selected_skills))
        if missing_selected:
            raise ContractError(
                f"{path}: selected distribution omits graph-required core skills: "
                + ", ".join(missing_selected)
            )
    entry_skill = document["entrySkill"]
    if entry_skill not in available_skills or entry_skill not in core_skills:
        raise ContractError(f"{path}.entrySkill: must name a mandatory core skill")

    raw_states = document["states"]
    if (
        not isinstance(raw_states, list)
        or len(raw_states) < 1
        or len(raw_states) > MAX_GRAPH_STATES
    ):
        raise ContractError(
            f"{path}.states: must contain between 1 and {MAX_GRAPH_STATES} states"
        )
    states: dict[str, dict[str, Any]] = {}
    for index, raw_state in enumerate(raw_states):
        label = f"{path}.states[{index}]"
        state = _object(
            raw_state,
            label,
            {"id", "ownerSkill", "actor", "commands", "transitions"},
        )
        identifier = state["id"]
        if (
            not isinstance(identifier, str)
            or SKILL_PATTERN.fullmatch(identifier) is None
            or identifier in states
        ):
            raise ContractError(f"{label}.id: invalid or duplicate state id")
        if state["actor"] not in {"automation", "human"}:
            raise ContractError(f"{label}.actor: must be automation or human")
        owner = state["ownerSkill"]
        if owner is not None and owner not in available_skills:
            raise ContractError(f"{label}.ownerSkill: unknown skill {owner}")
        if state["actor"] == "automation" and owner not in core_skills:
            raise ContractError(f"{label}.ownerSkill: automation owner must be core")
        if state["actor"] == "human" and owner is not None:
            raise ContractError(f"{label}.ownerSkill: human state must not have an owner skill")
        commands = state["commands"]
        if (
            not isinstance(commands, list)
            or len(commands) > MAX_GRAPH_COMMANDS
            or commands != sorted(set(commands))
        ):
            raise ContractError(f"{label}.commands: must be a sorted unique bounded list")
        unknown_commands = sorted(set(commands) - LIFECYCLE_COMMAND_PATHS)
        if unknown_commands:
            raise ContractError(
                f"{label}.commands: unknown processctl commands: "
                + ", ".join(unknown_commands)
            )
        transitions = state["transitions"]
        if (
            not isinstance(transitions, list)
            or len(transitions) < 1
            or len(transitions) > MAX_GRAPH_TRANSITIONS
        ):
            raise ContractError(f"{label}.transitions: invalid transition count")
        states[identifier] = state

    expected_states = set(PHASES) | EXTERNAL_STATES
    if set(states) != expected_states:
        missing = sorted(expected_states - set(states))
        extra = sorted(set(states) - expected_states)
        raise ContractError(
            f"{path}.states: must match lifecycle phases"
            + (f"; missing: {', '.join(missing)}" if missing else "")
            + (f"; unknown: {', '.join(extra)}" if extra else "")
        )

    for identifier, state in states.items():
        results: set[str] = set()
        for index, raw_transition in enumerate(state["transitions"]):
            label = f"{path}.states.{identifier}.transitions[{index}]"
            transition = _object(
                raw_transition,
                label,
                {"result", "nextState", "nextSkill"},
            )
            result = transition["result"]
            if (
                not isinstance(result, str)
                or SKILL_PATTERN.fullmatch(result) is None
                or result in results
            ):
                raise ContractError(f"{label}.result: invalid or duplicate result")
            results.add(result)
            next_state = transition["nextState"]
            next_skill = transition["nextSkill"]
            if next_state is None:
                if next_skill is not None:
                    raise ContractError(f"{label}: terminal transition cannot name a skill")
                continue
            if next_state not in states:
                raise ContractError(f"{label}.nextState: unknown state {next_state}")
            if next_skill is not None and next_skill not in core_skills:
                raise ContractError(f"{label}.nextSkill: must name a mandatory core skill")
    return document
