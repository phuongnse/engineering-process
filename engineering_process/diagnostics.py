from __future__ import annotations

import hashlib
import re
from typing import Any


DIAGNOSTIC_POLICY = "forbid-warning-error"
MAX_RECORDED_DIAGNOSTICS = 8

_TOOL_PREFIX = (
    r"(?:cmake|cargo|dotnet|go|gradle|maven|msbuild|npm|pip|pnpm|rustc|yarn)"
)

_WARNING_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        rf"^\s*{_TOOL_PREFIX}\s+warn(?:ing)?\b",
        r"^\s*(?:##)?\[\s*warn(?:ing)?\s*\]",
        r"^\s*warn(?:ing)?\b(?:\s*:|\s+\[|$)",
        r"^\s*[A-Za-z0-9_.+-]+\s+Warning\b(?:\s+at\b|\s*:|\s+\[|$)",
        r"^[^\r\n]{0,512}:\d+(?::\d+)?:\s*warning\b",
        r"\b[A-Za-z][A-Za-z0-9_.]*Warning\s*:",
        r'^\s*\{[^\r\n]{0,4096}"(?:level|severity)"\s*:\s*"(?:warn|warning)"',
        r"^\s*(?:level|severity)\s*[=:]\s*(?:warn|warning)\b",
    )
)

_ERROR_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        rf"^\s*{_TOOL_PREFIX}\s+error\b",
        r"^\s*(?:##)?\[\s*error\s*\]",
        r"^\s*error\b(?:\s*:|\s+\[|$)",
        r"^[^\r\n]{0,512}:\d+(?::\d+)?:\s*error\b",
        r"\b[A-Za-z][A-Za-z0-9_.]*Error\s*:",
        r'^\s*\{[^\r\n]{0,4096}"(?:level|severity)"\s*:\s*"error"',
        r"^\s*(?:level|severity)\s*[=:]\s*error\b",
    )
)


def _severity(line: str) -> str | None:
    if any(pattern.search(line) is not None for pattern in _ERROR_PATTERNS):
        return "error"
    if any(pattern.search(line) is not None for pattern in _WARNING_PATTERNS):
        return "warning"
    return None


def classify_diagnostics(
    *, stdout: bytes, stderr: bytes
) -> dict[str, Any]:
    """Classify bounded command output without retaining diagnostic text."""
    matches: list[dict[str, Any]] = []
    count = 0
    for stream, content in (("stdout", stdout), ("stderr", stderr)):
        text = content.decode("utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            severity = _severity(line)
            if severity is None:
                continue
            count += 1
            if len(matches) < MAX_RECORDED_DIAGNOSTICS:
                matches.append(
                    {
                        "severity": severity,
                        "stream": stream,
                        "line": line_number,
                        "lineSha256": hashlib.sha256(
                            line.encode("utf-8")
                        ).hexdigest(),
                    }
                )
    return {
        "policy": DIAGNOSTIC_POLICY,
        "status": "clean" if count == 0 else "failed",
        "count": count,
        "matches": matches,
        "matchesTruncated": count > len(matches),
    }
