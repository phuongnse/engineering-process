from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
import stat
import time

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
MAX_SKILL_ROOT_ENTRIES = 4_096
MAX_SELECTED_SKILLS = 256
MAX_SKILL_DOCUMENT_BYTES = 256_000
SKILL_ENUMERATION_TIMEOUT_SECONDS = 5.0


def skill_directories(root: Path) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise ContractError(f"{root}: skills root does not exist")
    deadline = time.monotonic() + SKILL_ENUMERATION_TIMEOUT_SECONDS
    directories: list[Path] = []
    try:
        with os.scandir(root) as iterator:
            for index, item in enumerate(iterator, start=1):
                if time.monotonic() >= deadline:
                    raise ContractError("skill enumeration exceeded 5 seconds")
                if index > MAX_SKILL_ROOT_ENTRIES:
                    raise ContractError(
                        f"skill root exceeds {MAX_SKILL_ROOT_ENTRIES} entries"
                    )
                metadata = item.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise ContractError(
                        f"skill root entry must not be a symlink: {item.path}"
                    )
                if not stat.S_ISDIR(metadata.st_mode):
                    continue
                skill = Path(item.path) / "SKILL.md"
                try:
                    skill_metadata = skill.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(skill_metadata.st_mode):
                    raise ContractError(f"skill document must not be a symlink: {skill}")
                if stat.S_ISREG(skill_metadata.st_mode):
                    directories.append(Path(item.path))
    except OSError as error:
        raise ContractError(f"cannot enumerate skill root {root}: {error}") from error
    return sorted(directories)


def _selected_skill_directories(
    root: Path, selected: tuple[str, ...] | None
) -> dict[str, Path]:
    if root.is_symlink() or not root.is_dir():
        raise ContractError(f"{root}: skills root does not exist")
    if selected is None:
        return {path.name: path for path in skill_directories(root)}
    if len(selected) > MAX_SELECTED_SKILLS:
        raise ContractError(
            f"selected skills exceed {MAX_SELECTED_SKILLS} items"
        )
    directories: dict[str, Path] = {}
    deadline = time.monotonic() + SKILL_ENUMERATION_TIMEOUT_SECONDS
    for name in selected:
        if time.monotonic() >= deadline:
            raise ContractError("selected skill inspection exceeded 5 seconds")
        directory = root / name
        skill = directory / "SKILL.md"
        try:
            directory_metadata = directory.lstat()
            skill_metadata = skill.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ContractError(f"cannot inspect selected skill {directory}: {error}") from error
        if (
            stat.S_ISDIR(directory_metadata.st_mode)
            and not stat.S_ISLNK(directory_metadata.st_mode)
            and stat.S_ISREG(skill_metadata.st_mode)
            and not stat.S_ISLNK(skill_metadata.st_mode)
        ):
            directories[name] = directory
    return directories


def _read_skill_text(path: Path) -> str:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ContractError(f"{path}: must be a regular non-symlink file")
        if before.st_size > MAX_SKILL_DOCUMENT_BYTES:
            raise ContractError(
                f"{path}: exceeds {MAX_SKILL_DOCUMENT_BYTES} bytes"
            )
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
            ):
                raise ContractError(f"{path}: changed while opening")
            content = stream.read(MAX_SKILL_DOCUMENT_BYTES + 1)
        after = path.lstat()
    except OSError as error:
        raise ContractError(f"{path}: cannot read: {error}") from error
    if (
        len(content) != before.st_size
        or len(content) > MAX_SKILL_DOCUMENT_BYTES
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_mode != before.st_mode
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
    ):
        raise ContractError(f"{path}: changed while reading")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError(f"{path}: cannot read UTF-8 content: {error}") from error
    return text.replace("\r\n", "\n").replace("\r", "\n")


def validate_skills(root: Path, selected: tuple[str, ...] | None = None) -> list[str]:
    directories = _selected_skill_directories(root, selected)
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
            text = _read_skill_text(path)
        except ContractError as error:
            issues.append(str(error))
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
    directories = _selected_skill_directories(root, selected)
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
