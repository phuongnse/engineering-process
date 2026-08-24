from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from typing import MutableMapping


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_ROOT = PROJECT_ROOT / "tests"
sys.path.insert(0, str(PROJECT_ROOT))
MAX_INHERITED_GIT_CONFIG_ENTRIES = 64
TEST_GIT_CONFIG = (
    ("core.autocrlf", "false"),
    ("core.safecrlf", "true"),
)


def configure_test_git_environment(environment: MutableMapping[str, str]) -> None:
    raw_count = environment.get("GIT_CONFIG_COUNT", "0")
    if not raw_count.isascii() or not raw_count.isdecimal():
        raise RuntimeError("GIT_CONFIG_COUNT must be a bounded decimal integer")
    count = int(raw_count)
    if count > MAX_INHERITED_GIT_CONFIG_ENTRIES:
        raise RuntimeError(
            "GIT_CONFIG_COUNT exceeds "
            f"{MAX_INHERITED_GIT_CONFIG_ENTRIES} inherited entries"
        )
    for index in range(count):
        for prefix in ("GIT_CONFIG_KEY", "GIT_CONFIG_VALUE"):
            name = f"{prefix}_{index}"
            value = environment.get(name)
            if value is None or not value or "\x00" in value or len(value) > 4096:
                raise RuntimeError(f"{name} must be a bounded non-empty value")
    for offset, (key, value) in enumerate(TEST_GIT_CONFIG, start=count):
        environment[f"GIT_CONFIG_KEY_{offset}"] = key
        environment[f"GIT_CONFIG_VALUE_{offset}"] = value
    environment["GIT_CONFIG_COUNT"] = str(count + len(TEST_GIT_CONFIG))


def main() -> int:
    try:
        configure_test_git_environment(os.environ)
    except RuntimeError as error:
        print(f"test suite environment failed: {error}", file=sys.stderr)
        return 2
    suite = unittest.defaultTestLoader.discover(
        str(TEST_ROOT), pattern="test_*.py"
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
