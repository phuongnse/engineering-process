from __future__ import annotations


# Portable command paths referenced by the lifecycle graph. Parser conformance tests
# prove that every entry remains routable by processctl.
LIFECYCLE_COMMAND_PATHS = frozenset(
    {
        "change finish",
        "change decision resolve",
        "change decision start",
        "change decision submit",
        "change implement",
        "change plan",
        "change remote ingest",
        "change remote request",
        "change review start",
        "change review submit",
        "change start",
        "change verify",
        "contract validate",
        "evidence encode-completion",
        "improvement attach",
        "improvement classify",
        "improvement export-signal",
        "improvement status",
        "publication validate-range",
        "publication validate-evidence-source",
        "publication validate-proposal-completion",
        "publication validate-source",
    }
)
