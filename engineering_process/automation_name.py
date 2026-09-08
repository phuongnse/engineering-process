"""Compose automation names from a selected convention and consumer-owned components."""

from pathlib import Path
from typing import Any

from .artifact_standards import ArtifactStandard
from .contracts import ProcessError, validate_document
from .distribution import distribution_root, schemas_root


def render_name(
    standard: ArtifactStandard,
    data: dict[str, Any],
    *,
    state: str = "ready",
    process_root: Path | None = None,
) -> str:
    if standard.document["adapter"] != "automation-name":
        raise ProcessError("selected standard requires a different name adapter")
    validate_document(data, "automation-name-data", schema_root=schemas_root(distribution_root(process_root)))
    if state not in {"draft", "ready"}:
        raise ProcessError("artifact state must be draft or ready")
    rules = standard.rules
    if set(data["components"]) != set(rules["components"]):
        raise ProcessError("name components must exactly match the selected standard")
    name = rules["separator"].join(data["components"][key] for key in rules["components"])
    if rules["case"] == "lower":
        name = name.lower()
    if len(name) > rules["maxLength"]:
        raise ProcessError("automation name exceeds the selected length limit")
    return name + "\n"
