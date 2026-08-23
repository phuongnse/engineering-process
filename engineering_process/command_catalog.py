from __future__ import annotations


# Portable command paths referenced by the lifecycle graph. Parser conformance tests
# prove that every entry remains routable by processctl.
LIFECYCLE_COMMAND_PATHS = frozenset(
    {
        "change finish",
        "change implement",
        "change plan",
        "change review start",
        "change review submit",
        "change start",
        "change verify",
        "contract validate",
        "publication validate-range",
        "publication validate-source",
    }
)
