from __future__ import annotations

from pathlib import Path

from .contracts import ContractError, SKILL_PATTERN, read_json
from .skills import skill_directories


MANDATORY_BUNDLE = "core"


def bundles_path(process_root: Path) -> Path:
    candidates = (
        process_root / "bundles.json",
        process_root / "share" / "engineering-process" / "bundles.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ContractError(f"{process_root}: cannot locate bundles.json")


def load_bundles(process_root: Path, skills_root: Path) -> dict[str, tuple[str, ...]]:
    path = bundles_path(process_root)
    document = read_json(path)
    if (
        not isinstance(document, dict)
        or set(document) != {"schemaVersion", "bundles"}
        or document.get("schemaVersion") != 1
        or not isinstance(document.get("bundles"), dict)
        or not document["bundles"]
    ):
        raise ContractError(f"{path}: invalid bundle contract")

    available = {directory.name for directory in skill_directories(skills_root)}
    result: dict[str, tuple[str, ...]] = {}
    owned: dict[str, str] = {}
    for name, raw_skills in document["bundles"].items():
        if SKILL_PATTERN.fullmatch(name) is None:
            raise ContractError(f"{path}: invalid bundle name {name}")
        if (
            not isinstance(raw_skills, list)
            or not raw_skills
            or not all(
                isinstance(skill, str) and SKILL_PATTERN.fullmatch(skill)
                for skill in raw_skills
            )
            or raw_skills != sorted(set(raw_skills))
        ):
            raise ContractError(f"{path}: bundle {name} must be a sorted unique skill list")
        missing = sorted(set(raw_skills) - available)
        if missing:
            raise ContractError(
                f"{path}: bundle {name} references missing skills: {', '.join(missing)}"
            )
        for skill in raw_skills:
            previous = owned.get(skill)
            if previous is not None:
                raise ContractError(
                    f"{path}: skill {skill} belongs to both {previous} and {name}"
                )
            owned[skill] = name
        result[name] = tuple(raw_skills)
    unowned = sorted(available - set(owned))
    if unowned:
        raise ContractError(
            f"{path}: skills missing bundle ownership: {', '.join(unowned)}"
        )
    return result


def select_bundles(
    available: dict[str, tuple[str, ...]], requested: list[str] | None
) -> tuple[str, ...]:
    if MANDATORY_BUNDLE not in available:
        raise ContractError("bundle catalog is missing the mandatory core bundle")
    selected = {MANDATORY_BUNDLE, *(requested or [])}
    unknown = sorted(selected - set(available))
    if unknown:
        raise ContractError(f"unknown bundles: {', '.join(unknown)}")
    return tuple(sorted(selected))
