#!/usr/bin/env python3
"""Run the complete deterministic unit and contract suite."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time
import unittest
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SLOW_TEST_LIMIT = 10


class TimingTestResult(unittest.TextTestResult):
    """Keep bounded test timing metadata for local failure diagnosis."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._started: dict[unittest.case.TestCase, float] = {}
        self.timings: list[tuple[float, str]] = []

    def startTest(self, test: unittest.case.TestCase) -> None:
        self._started[test] = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test: unittest.case.TestCase) -> None:
        started = self._started.pop(test, None)
        if started is not None:
            self.timings.append((time.perf_counter() - started, test.id()))
        super().stopTest(test)


class TimingTextTestRunner(unittest.TextTestRunner):
    resultclass = TimingTestResult

    def run(self, test: unittest.suite.TestSuite) -> TimingTestResult:
        started = time.perf_counter()
        result = super().run(test)
        elapsed = time.perf_counter() - started
        self.stream.writeln(
            "Suite timing: "
            f"{elapsed:.3f}s; tests={result.testsRun}; "
            f"failures={len(result.failures)}; errors={len(result.errors)}; "
            f"skipped={len(result.skipped)}; "
            f"test_cases={sum(duration for duration, _ in result.timings):.3f}s"
        )
        for duration, test_id in sorted(result.timings, reverse=True)[:SLOW_TEST_LIMIT]:
            self.stream.writeln(f"Slow test: {duration:.3f}s {test_id}")
        return result


def _load_suite(modules: Sequence[str]) -> unittest.TestSuite:
    loader = unittest.defaultTestLoader
    if not modules:
        return loader.discover(str(PROJECT_ROOT / "tests"))
    suite = unittest.TestSuite()
    for module in modules:
        suite.addTests(loader.loadTestsFromName(module))
    return suite


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the deterministic unit and contract suite"
    )
    parser.add_argument(
        "--module",
        action="append",
        default=[],
        help="load one test module; repeat to run a bounded unit group",
    )
    args = parser.parse_args(argv)
    sys.path.insert(0, str(PROJECT_ROOT))
    suite = _load_suite(args.module)
    if suite.countTestCases() == 0:
        parser.error("at least one selected module must contain tests")
    result = TimingTextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
