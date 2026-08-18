from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .contracts import ContractError, SKILL_PATTERN


FRONTMATTER = re.compile(
    r"\A---\n(?P<header>.*?)\n---\n(?P<body>.*)\Z",
    re.DOTALL,
)
FORBIDDEN_CORE_TERMS = (
    "apply_patch",
    "claude",
    "codex",
    "cursor",
    "gemini",
    "gpt-",
    "reasoning_effort",
    "spawn_agent",
    "subagent",
)
MARKER_NAME = ".engineering-process.json"


def skill_directories(root: Path) -> list[Path]:
    if not root.is_dir():
        raise ContractError(f"{root}: skills root does not exist")
    return sorted(
        path for path in root.iterdir() if path.is_dir() and (path / "SKILL.md").is_file()
    )


def validate_skills(root: Path, selected: tuple[str, ...] | None = None) -> list[str]:
    directories = {path.name: path for path in skill_directories(root)}
    names = tuple(sorted(directories)) if selected is None else selected
    issues: list[str] = []
    missing = sorted(set(names) - set(directories))
    if missing:
        issues.append(f"{root}: missing skills: {', '.join(missing)}")

    for name in names:
        directory = directories.get(name)
        if directory is None:
            continue
        if SKILL_PATTERN.fullmatch(name) is None or len(name) > 64:
            issues.append(f"{directory}: invalid skill directory name")
        path = directory / "SKILL.md"
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            issues.append(f"{path}: cannot read UTF-8 content: {error}")
            continue
        if text.startswith("\ufeff"):
            issues.append(f"{path}: UTF-8 BOM is not allowed")
        if "\r" in text:
            issues.append(f"{path}: use LF line endings")
        if "TODO" in text or "[TODO" in text:
            issues.append(f"{path}: unresolved template marker")
        if len(text.splitlines()) > 500:
            issues.append(f"{path}: exceeds the 500-line portability limit")

        match = FRONTMATTER.match(text)
        if match is None:
            issues.append(f"{path}: invalid YAML frontmatter boundary")
            continue
        header: dict[str, str] = {}
        for line_number, line in enumerate(match.group("header").splitlines(), 2):
            if not line or line.startswith((" ", "\t", "#")) or ":" not in line:
                issues.append(f"{path}:{line_number}: unsupported frontmatter syntax")
                continue
            key, value = line.split(":", 1)
            if key in header:
                issues.append(f"{path}:{line_number}: duplicate frontmatter key {key}")
            header[key] = value.strip()
        extra = sorted(set(header) - {"name", "description"})
        if extra:
            issues.append(
                f"{path}: core skills allow only name and description; found {extra}"
            )
        if header.get("name") != name:
            issues.append(f"{path}: frontmatter name must match {name}")
        description = header.get("description", "")
        if not description or len(description) > 1024:
            issues.append(f"{path}: description must contain 1 to 1024 characters")
        if not match.group("body").lstrip().startswith("# "):
            issues.append(f"{path}: body must start with an H1")

        lowered = text.lower()
        for term in FORBIDDEN_CORE_TERMS:
            if term in lowered:
                issues.append(f"{path}: agent-specific core term is forbidden: {term}")
        for line_number, line in enumerate(text.splitlines(), 1):
            if re.search(r"(?:^|[\s(])/(?:home|users|workspace|tmp)/", line.lower()):
                issues.append(f"{path}:{line_number}: absolute local path is forbidden")
    return issues


def skill_digest(root: Path, selected: tuple[str, ...] | None = None) -> str:
    issues = validate_skills(root, selected)
    if issues:
        raise ContractError("\n".join(issues))
    directories = {path.name: path for path in skill_directories(root)}
    names = tuple(sorted(directories)) if selected is None else selected
    digest = hashlib.sha256()
    for name in names:
        directory = directories[name]
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            if path.name == MARKER_NAME:
                continue
            relative = path.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"
