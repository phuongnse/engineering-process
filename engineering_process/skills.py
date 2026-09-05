"""Portable Agent Skills validation and routing checks."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from .contracts import ProcessError, load_and_validate
from .distribution import schemas_root


NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
MAX_SKILL_BYTES = 128_000


def _frontmatter(path: Path) -> tuple[str, str, str]:
    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise ProcessError(f"{path}: cannot read UTF-8 skill: {error}") from error
    if len(data) > MAX_SKILL_BYTES:
        raise ProcessError(f"{path}: skill exceeds {MAX_SKILL_BYTES} bytes")
    if "\r" in text:
        raise ProcessError(f"{path}: skills must use LF line endings")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ProcessError(f"{path}: missing Agent Skills frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise ProcessError(f"{path}: unterminated frontmatter") from error
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            raise ProcessError(f"{path}: invalid frontmatter line: {line!r}")
        key, value = line.split(":", 1)
        if key not in {"name", "description"} or key in values:
            raise ProcessError(f"{path}: unsupported or repeated frontmatter key {key!r}")
        values[key] = value.strip()
    if set(values) != {"name", "description"}:
        raise ProcessError(f"{path}: frontmatter requires name and description")
    if not values["description"] or len(values["description"]) > 1024:
        raise ProcessError(f"{path}: description must contain 1-1024 characters")
    return values["name"], values["description"], text


def _validate_links(path: Path, text: str) -> None:
    for target in LINK.findall(text):
        if target.startswith(("http://", "https://", "#")):
            continue
        relative = target.split("#", 1)[0]
        if not relative:
            continue
        destination = (path.parent / relative).resolve()
        try:
            destination.relative_to(path.parent.resolve())
        except ValueError as error:
            raise ProcessError(f"{path}: local link escapes its skill: {target}") from error
        if not destination.is_file():
            raise ProcessError(f"{path}: local link does not exist: {target}")


def validate_skills(
    root: Path,
    *,
    process_root: Path,
) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise ProcessError(f"{root}: skills root does not exist")
    names: list[str] = []
    texts: dict[str, str] = {}
    for directory in sorted(
        path for path in root.iterdir() if path.is_dir() and (path / "SKILL.md").is_file()
    ):
        skill_path = directory / "SKILL.md"
        name, _description, text = _frontmatter(skill_path)
        if not NAME.fullmatch(name) or name != directory.name:
            raise ProcessError(f"{skill_path}: name must match directory {directory.name}")
        _validate_links(skill_path, text)
        names.append(name)
        texts[name] = text

    if len(names) != len(set(names)):
        raise ProcessError("skill names must be unique")
    graph_path = process_root / "process-graph.json"
    graph = load_and_validate(
        graph_path, "process-graph", schema_root=schemas_root(process_root)
    )
    owners = {state["id"]: state["ownerSkill"] for state in graph["states"]}
    if len(owners) != len(graph["states"]):
        raise ProcessError("process graph state ids must be unique")
    for state in graph["states"]:
        for transition in state["transitions"]:
            destination = transition["nextState"]
            skill = transition["nextSkill"]
            if (destination is None) != (skill is None):
                raise ProcessError("process graph terminal transitions must have both targets null")
            if destination is not None and destination not in owners:
                raise ProcessError(f"process graph references missing state: {destination}")
            if skill is not None and skill not in names:
                raise ProcessError(
                    f"process graph transition from {state['id']} references missing skill: {skill}"
                )
    routed = {graph["entrySkill"]}
    routed.update(state["ownerSkill"] for state in graph["states"])
    routed.update(graph.get("specializations", {}).values())
    unknown = sorted(routed - set(names))
    orphaned = sorted(set(names) - routed)
    if unknown:
        raise ProcessError("process graph references missing skills: " + ", ".join(unknown))
    if orphaned:
        raise ProcessError("skills are not reachable from the process graph: " + ", ".join(orphaned))
    entry_text = texts[graph["entrySkill"]]
    unmentioned = sorted(name for name in names if name != graph["entrySkill"] and name not in entry_text)
    if unmentioned:
        raise ProcessError(
            "entry skill does not describe routes to: " + ", ".join(unmentioned)
        )
    return {"skills": names, "entrySkill": graph["entrySkill"], "count": len(names)}
