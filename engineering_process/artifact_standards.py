"""Resolve one versioned artifact contract for both generation and verification."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any

from .contracts import ProcessError, digest_json, load_and_validate
from .distribution import schemas_root
from .repository import STATE_PREFIXES, _git


IDENTIFIER = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")
MAX_DOCUMENT_BYTES = 1_000_000


@dataclass(frozen=True)
class ArtifactStandard:
    document: dict[str, Any]
    source: str

    @property
    def rules(self) -> dict[str, Any]:
        return self.document["rules"]

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            **{key: self.document[key] for key in ("id", "version", "artifact", "adapter")},
            "digest": digest_json(self.document),
            "source": self.source,
        }

    def unresolved(self, value: str) -> bool:
        # These are reserved values in an owned protocol, not prose classification.
        return value.strip().casefold() in {
            item.strip().casefold() for item in self.document.get("pendingValues", [])
        }


def _consumer_file(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise ProcessError(f"standard path escapes the consumer repository: {relative}")
    if path.resolve() != path or Path(relative).as_posix() != relative:
        raise ProcessError(f"standard path must be canonical without links: {relative}")
    if any(part in {"..", ".git"} for part in Path(relative).parts):
        raise ProcessError(f"standard path is not an owned repository file: {relative}")
    current = root
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink():
            raise ProcessError(f"standard paths cannot traverse symlinks: {relative}")
    if not path.is_file():
        raise ProcessError(f"consumer standard file is missing: {relative}")
    encoded = os.fsencode(Path(relative).as_posix())
    if any(encoded.startswith(prefix) for prefix in STATE_PREFIXES):
        raise ProcessError(f"standard file is excluded from lifecycle snapshots: {relative}")
    inventory = _git(root, ["ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", relative])
    if encoded not in inventory.split(b"\0"):
        raise ProcessError(f"standard file must be included in the consumer Git snapshot: {relative}")
    return path


def _unique(values: list[str], description: str) -> None:
    if len(values) != len(set(values)):
        raise ProcessError(f"artifact standard requires unique {description}")


def _validate_relations(document: dict[str, Any]) -> None:
    _unique([value.strip().casefold() for value in document.get("pendingValues", [])], "pending values")
    rules = document["rules"]
    if document["adapter"] == "pr-description":
        sections = rules["sections"]
        _unique([section["heading"] for section in sections], "section headings")
        for kind in ("fields", "checks"):
            entries = [entry for section in sections for entry in section[kind]]
            _unique([entry["id"] for entry in entries], f"{kind} ids")
            _unique([entry["label"] for entry in entries], f"{kind} labels")
    elif document["adapter"] == "release-notes":
        _unique([group["type"] for group in rules["groups"]], "change types")
        _unique([section["id"] for section in rules["sections"]], "release section ids")
        _unique([item["heading"] for item in rules["groups"] + rules["sections"]], "release headings")
    elif document["adapter"] == "issue":
        for state, definition in rules["states"].items():
            sections = definition["sections"]
            _unique([section["heading"] for section in sections], f"{state} issue section headings")
            for kind in ("fields", "checks"):
                entries = [entry for section in sections for entry in section[kind]]
                _unique([entry["id"] for entry in entries], f"{state} issue {kind} ids")
                _unique([entry["label"] for entry in entries], f"{state} issue {kind} labels")


def read_document(path: Path) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ProcessError("artifact body exceeds its size limit")
    data.decode("utf-8")
    return data


@dataclass(frozen=True)
class StandardCatalog:
    """One selection snapshot for adapter resolution and adoption ownership checks."""

    project_root: Path | None
    process_root: Path
    selections: dict[str, Any]
    consumer_files: frozenset[Path]

    def resolve(self, artifact: str) -> ArtifactStandard:
        if len(artifact) > 128 or not IDENTIFIER.fullmatch(artifact):
            raise ProcessError("artifact must be a canonical identifier")
        selection = self.selections.get(artifact, {"builtin": f"{artifact}@1"})
        if "path" in selection:
            path = _consumer_file(self.project_root, selection["path"])
            source = selection["path"]
        else:
            name, version = selection["builtin"].rsplit("@", 1)
            path = self.process_root / "process_assets" / "standards" / f"{name}.v{version}.json"
            source = selection["builtin"]
            if path.is_symlink() or not path.is_file():
                raise ProcessError(f"unsupported packaged artifact standard: {source}")
        document = load_and_validate(path, "artifact-standard", schema_root=schemas_root(self.process_root))
        if document["artifact"] != artifact:
            raise ProcessError(f"selected standard is for {document['artifact']}, not {artifact}")
        if "builtin" in selection and (name != artifact or document["version"] != int(version)):
            raise ProcessError("packaged standard identity does not match its selection")
        _validate_relations(document)
        return ArtifactStandard(document, source)


def load_standard_catalog(project_root: Path | None, process_root: Path) -> StandardCatalog:
    selections: dict[str, Any] = {}
    consumer_files: set[Path] = set()
    if project_root is not None:
        project_root = project_root.resolve()
        selector = project_root / ".process" / "standards.json"
        if selector.exists() or selector.is_symlink():
            document = load_and_validate(
                _consumer_file(project_root, ".process/standards.json"),
                "artifact-selection", schema_root=schemas_root(process_root),
            )
            selections = document["artifacts"]
            consumer_files = {Path(".process/standards.json")}
            consumer_files.update(Path(value["path"]) for value in selections.values() if "path" in value)
    return StandardCatalog(project_root, process_root, selections, frozenset(consumer_files))


def resolve_standard(project_root: Path | None, process_root: Path, artifact: str) -> ArtifactStandard:
    return load_standard_catalog(project_root, process_root).resolve(artifact)
