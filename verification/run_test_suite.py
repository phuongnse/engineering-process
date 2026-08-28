from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
import time
import unittest
from typing import Any, BinaryIO, Callable, MutableMapping


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_ROOT = PROJECT_ROOT / "tests"
sys.path.insert(0, str(PROJECT_ROOT))
MAX_INHERITED_GIT_CONFIG_ENTRIES = 64
MAX_TIMED_TESTS = 10_000
MAX_TEST_IDENTIFIER_CHARACTERS = 512
MAX_MODULE_IDENTIFIER_CHARACTERS = 256
MAX_TIMING_REPORT_BYTES = 2_000_000
SLOWEST_TEST_LIMIT = 50
TIMING_OUTCOMES = (
    "error",
    "expected-failure",
    "failed",
    "passed",
    "skipped",
    "unexpected-success",
)
OUTCOME_PRIORITY = {
    "passed": 0,
    "skipped": 1,
    "expected-failure": 2,
    "failed": 3,
    "error": 4,
    "unexpected-success": 5,
}
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


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _bounded_identity(value: object, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise RuntimeError(
            f"{label} must contain between 1 and {maximum} characters"
        )
    if any(ord(character) < 0x20 for character in value):
        raise RuntimeError(f"{label} contains a control character")
    return value


class TimingTextTestResult(unittest.TextTestResult):
    """Record diagnostic timings without replacing unittest result authority."""

    def __init__(
        self,
        stream: Any,
        descriptions: bool,
        verbosity: int,
        *,
        clock_ns: Callable[[], int] | None = None,
    ) -> None:
        super().__init__(stream, descriptions, verbosity)
        self._clock_ns = clock_ns or time.perf_counter_ns
        self._timing_starts: dict[int, tuple[unittest.TestCase, int]] = {}
        self._timing_outcomes: dict[int, str] = {}
        self.timing_records: list[dict[str, Any]] = []
        self.timing_limit_exceeded = False

    def startTest(self, test: unittest.TestCase) -> None:  # noqa: N802
        super().startTest(test)
        if len(self.timing_records) + len(self._timing_starts) >= MAX_TIMED_TESTS:
            self.timing_limit_exceeded = True
            return
        self._timing_starts[id(test)] = (test, self._clock_ns())

    def _set_outcome(self, test: unittest.TestCase, outcome: str) -> None:
        token = id(test)
        if token not in self._timing_starts:
            return
        current = self._timing_outcomes.get(token)
        if current is None or OUTCOME_PRIORITY[outcome] > OUTCOME_PRIORITY[current]:
            self._timing_outcomes[token] = outcome

    def addSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        super().addSuccess(test)
        self._set_outcome(test, "passed")

    def addError(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        super().addError(test, err)
        self._set_outcome(test, "error")

    def addFailure(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        super().addFailure(test, err)
        self._set_outcome(test, "failed")

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:  # noqa: N802
        super().addSkip(test, reason)
        self._set_outcome(test, "skipped")

    def addExpectedFailure(  # noqa: N802
        self, test: unittest.TestCase, err: Any
    ) -> None:
        super().addExpectedFailure(test, err)
        self._set_outcome(test, "expected-failure")

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        super().addUnexpectedSuccess(test)
        self._set_outcome(test, "unexpected-success")

    def addSubTest(  # noqa: N802
        self, test: unittest.TestCase, subtest: unittest.TestCase, err: Any
    ) -> None:
        super().addSubTest(test, subtest, err)
        if err is not None:
            failure = isinstance(err, tuple) and issubclass(
                err[0], test.failureException
            )
            self._set_outcome(test, "failed" if failure else "error")

    def stopTest(self, test: unittest.TestCase) -> None:  # noqa: N802
        ended_ns = self._clock_ns()
        started = self._timing_starts.pop(id(test), None)
        if started is not None:
            recorded_test, started_ns = started
            self.timing_records.append(
                {
                    "id": recorded_test.id(),
                    "module": recorded_test.__class__.__module__,
                    "outcome": self._timing_outcomes.pop(id(test), "passed"),
                    "durationNs": max(0, ended_ns - started_ns),
                }
            )
        super().stopTest(test)


def _outcome_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts = {outcome: 0 for outcome in TIMING_OUTCOMES}
    for record in records:
        outcome = record.get("outcome")
        if outcome not in counts:
            raise RuntimeError(f"timing record has an invalid outcome: {outcome!r}")
        counts[outcome] += 1
    return counts


def build_timing_report(
    result: TimingTextTestResult,
    *,
    started_at: str,
    completed_at: str,
    duration_ns: int,
) -> dict[str, Any]:
    if result.timing_limit_exceeded:
        raise RuntimeError(f"timed test count exceeds {MAX_TIMED_TESTS}")
    records = result.timing_records
    if len(records) != result.testsRun:
        raise RuntimeError(
            "timing record count does not match unittest executed test count"
        )
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    module_records: dict[str, list[dict[str, Any]]] = {}
    for position, record in enumerate(records):
        identifier = _bounded_identity(
            record.get("id"),
            f"timing test identity at position {position}",
            maximum=MAX_TEST_IDENTIFIER_CHARACTERS,
        )
        if identifier in seen:
            raise RuntimeError(f"timing report contains duplicate test id: {identifier}")
        seen.add(identifier)
        module = _bounded_identity(
            record.get("module"),
            f"timing module identity for {identifier}",
            maximum=MAX_MODULE_IDENTIFIER_CHARACTERS,
        )
        duration = record.get("durationNs")
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
            raise RuntimeError(f"timing duration is invalid for {identifier}")
        row = {
            "id": identifier,
            "module": module,
            "outcome": record.get("outcome"),
            "durationMs": duration // 1_000_000,
        }
        _outcome_counts([row])
        normalized.append(row)
        ranked.append((duration, identifier, row))
        module_records.setdefault(module, []).append(
            {**row, "durationNs": duration}
        )

    modules = [
        {
            "id": module,
            "testCount": len(module_rows),
            "durationMs": sum(row["durationNs"] for row in module_rows)
            // 1_000_000,
            "outcomes": _outcome_counts(module_rows),
        }
        for module, module_rows in sorted(module_records.items())
    ]
    slowest = [
        row
        for _duration, _identifier, row in sorted(
            ranked, key=lambda item: (-item[0], item[1])
        )[:SLOWEST_TEST_LIMIT]
    ]
    if (
        isinstance(duration_ns, bool)
        or not isinstance(duration_ns, int)
        or duration_ns < 0
    ):
        raise RuntimeError("timing suite duration is invalid")
    return {
        "schemaVersion": 1,
        "kind": "engineering-process-unittest-timing",
        "authority": "diagnostic-only",
        "status": "passed" if result.wasSuccessful() else "failed",
        "startedAt": started_at,
        "completedAt": completed_at,
        "durationMs": duration_ns // 1_000_000,
        "testCount": len(normalized),
        "moduleCount": len(modules),
        "outcomes": _outcome_counts(normalized),
        "tests": normalized,
        "modules": modules,
        "slowest": slowest,
        "slowestTruncated": len(normalized) > SLOWEST_TEST_LIMIT,
    }


def _timing_bytes(report: dict[str, Any]) -> bytes:
    try:
        content = (
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"timing report is not JSON serializable: {error}") from error
    if len(content) > MAX_TIMING_REPORT_BYTES:
        raise RuntimeError(
            "timing report exceeds "
            f"{MAX_TIMING_REPORT_BYTES} bytes: {len(content)}"
        )
    return content


def duplicate_timing_descriptor(descriptor: int) -> int:
    if isinstance(descriptor, bool) or not isinstance(descriptor, int) or descriptor < 3:
        raise RuntimeError("timing output descriptor must be an integer of at least 3")
    duplicate = -1
    try:
        duplicate = os.dup(descriptor)
        os.set_inheritable(duplicate, False)
    except BaseException as error:
        primary_error = error
        if duplicate >= 0:
            try:
                os.close(duplicate)
            except BaseException as close_error:
                primary_error = _error_with_cleanup(
                    primary_error,
                    close_error,
                    label="timing output duplicate cleanup also failed",
                )
        if primary_error is not error:
            raise primary_error from error
        if not isinstance(error, (OSError, ValueError)):
            raise
        raise RuntimeError(
            f"cannot duplicate timing output descriptor: {_error_message(error)}"
        ) from error
    return duplicate


def write_timing_stream(stream: BinaryIO, report: dict[str, Any]) -> None:
    content = _timing_bytes(report)
    view = memoryview(content)
    offset = 0
    try:
        while offset < len(content):
            written = stream.write(view[offset:])
            if (
                isinstance(written, bool)
                or not isinstance(written, int)
                or written <= 0
                or written > len(content) - offset
            ):
                raise RuntimeError("timing output stream made no valid write progress")
            offset += written
        stream.flush()
    except (OSError, ValueError) as error:
        raise RuntimeError(f"cannot write timing output stream: {error}") from error


def write_timing_descriptor(descriptor: int, report: dict[str, Any]) -> None:
    stream: BinaryIO | None = None
    primary_error: BaseException | None = None
    try:
        stream = os.fdopen(descriptor, "wb", closefd=True)
        descriptor = -1
        write_timing_stream(stream, report)
    except BaseException as error:
        primary_error = error
    try:
        if stream is not None:
            stream.close()
        elif descriptor >= 0:
            os.close(descriptor)
    except BaseException as close_error:
        if primary_error is not None:
            primary_error = _error_with_cleanup(
                primary_error,
                close_error,
                label="timing output duplicate close also failed",
            )
        else:
            primary_error = close_error
    if primary_error is not None:
        if isinstance(primary_error, (OSError, ValueError)):
            raise RuntimeError(
                f"cannot close timing output duplicate: {primary_error}"
            ) from primary_error
        raise primary_error


def _close_duplicate_after_error(
    descriptor: int, primary_error: BaseException
) -> None:
    try:
        os.close(descriptor)
    except BaseException as close_error:
        selected = _error_with_cleanup(
            primary_error,
            close_error,
            label="timing output duplicate close also failed",
        )
        if selected is not primary_error:
            raise selected from primary_error


def _error_with_cleanup(
    primary_error: BaseException,
    cleanup_error: BaseException,
    *,
    label: str,
) -> BaseException:
    primary_controls_flow = not isinstance(primary_error, Exception)
    cleanup_controls_flow = not isinstance(cleanup_error, Exception)
    if cleanup_controls_flow and not primary_controls_flow:
        cleanup_error.add_note(
            f"earlier timing output operation also failed: {_error_message(primary_error)}"
        )
        return cleanup_error
    primary_error.add_note(f"{label}: {_error_message(cleanup_error)}")
    return primary_error


def _error_message(error: BaseException) -> str:
    details = [str(error)]
    details.extend(getattr(error, "__notes__", ()))
    return "; ".join(detail for detail in details if detail)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the complete unittest suite")
    parser.add_argument(
        "--timing-output-fd",
        type=int,
        help="Write bounded diagnostic timings to this coordinator-owned open fd",
    )
    args = parser.parse_args(argv)
    try:
        configure_test_git_environment(os.environ)
    except RuntimeError as error:
        print(f"test suite environment failed: {error}", file=sys.stderr)
        return 2
    suite = unittest.defaultTestLoader.discover(
        str(TEST_ROOT), pattern="test_*.py"
    )
    if args.timing_output_fd is None:
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        return 0 if result.wasSuccessful() else 1

    try:
        duplicate = duplicate_timing_descriptor(args.timing_output_fd)
    except RuntimeError as error:
        print(f"test timing stream failed: {_error_message(error)}", file=sys.stderr)
        return 2
    try:
        started_at = _timestamp()
        started_ns = time.perf_counter_ns()
        result = unittest.TextTestRunner(
            verbosity=1, resultclass=TimingTextTestResult
        ).run(suite)
        completed_ns = time.perf_counter_ns()
        report = build_timing_report(
            result,
            started_at=started_at,
            completed_at=_timestamp(),
            duration_ns=max(0, completed_ns - started_ns),
        )
    except BaseException as error:
        _close_duplicate_after_error(duplicate, error)
        if isinstance(error, RuntimeError):
            print(
                f"test timing stream failed: {_error_message(error)}",
                file=sys.stderr,
            )
            return 2
        raise
    try:
        write_timing_descriptor(duplicate, report)
    except RuntimeError as error:
        print(f"test timing stream failed: {_error_message(error)}", file=sys.stderr)
        return 2
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
