from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from verification.run_test_suite import (
    MAX_TEST_IDENTIFIER_CHARACTERS,
    MAX_TIMING_REPORT_BYTES,
    TimingTextTestResult,
    build_timing_report,
    configure_test_git_environment,
    duplicate_timing_descriptor,
    main,
    write_timing_descriptor,
    write_timing_stream,
)


class TestSuiteRunnerTests(unittest.TestCase):
    @staticmethod
    def _empty_timing_result() -> TimingTextTestResult:
        return TimingTextTestResult(io.StringIO(), True, 1)

    @staticmethod
    def _passing_suite() -> unittest.TestSuite:
        class PassingFixture(unittest.TestCase):
            def test_passes(self):
                self.assertTrue(True)

        return unittest.defaultTestLoader.loadTestsFromTestCase(PassingFixture)

    def test_appends_deterministic_fixture_git_config(self):
        environment = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.autocrlf",
            "GIT_CONFIG_VALUE_0": "true",
        }

        configure_test_git_environment(environment)

        self.assertEqual("3", environment["GIT_CONFIG_COUNT"])
        self.assertEqual("core.autocrlf", environment["GIT_CONFIG_KEY_1"])
        self.assertEqual("false", environment["GIT_CONFIG_VALUE_1"])
        self.assertEqual("core.safecrlf", environment["GIT_CONFIG_KEY_2"])
        self.assertEqual("true", environment["GIT_CONFIG_VALUE_2"])

    def test_rejects_unbounded_or_incomplete_inherited_git_config(self):
        with self.assertRaisesRegex(RuntimeError, "bounded decimal"):
            configure_test_git_environment({"GIT_CONFIG_COUNT": "invalid"})
        with self.assertRaisesRegex(RuntimeError, "exceeds 64"):
            configure_test_git_environment({"GIT_CONFIG_COUNT": "65"})
        with self.assertRaisesRegex(RuntimeError, "GIT_CONFIG_KEY_0"):
            configure_test_git_environment({"GIT_CONFIG_COUNT": "1"})

    def test_conflicting_inherited_autocrlf_emits_no_fixture_warning(self):
        environment = {
            **os.environ,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.autocrlf",
            "GIT_CONFIG_VALUE_0": "true",
        }
        configure_test_git_environment(environment)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(
                ["git", "init", "-q"], cwd=root, env=environment, check=True
            )
            (root / "fixture.txt").write_bytes(b"line\n")
            result = subprocess.run(
                ["git", "add", "fixture.txt"],
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(0, result.returncode)
        self.assertEqual(b"", result.stderr)

    def test_records_every_unittest_outcome_without_replacing_result_authority(self):
        class OutcomeFixture(unittest.TestCase):
            def test_error(self):
                raise RuntimeError("fixture failure")

            @unittest.expectedFailure
            def test_expected_failure(self):
                self.fail("expected fixture failure")

            def test_failure(self):
                self.fail("fixture failure")

            def test_pass(self):
                self.assertTrue(True)

            @unittest.skip("fixture skip")
            def test_skip(self):
                self.fail("not executed")

            @unittest.expectedFailure
            def test_unexpected_success(self):
                self.assertTrue(True)

        result = unittest.TextTestRunner(
            stream=io.StringIO(),
            verbosity=1,
            resultclass=TimingTextTestResult,
        ).run(unittest.defaultTestLoader.loadTestsFromTestCase(OutcomeFixture))

        report = build_timing_report(
            result,
            started_at="2026-08-28T00:00:00Z",
            completed_at="2026-08-28T00:00:01Z",
            duration_ns=1_000_000_000,
        )

        self.assertFalse(result.wasSuccessful())
        self.assertEqual("failed", report["status"])
        self.assertEqual(6, report["testCount"])
        self.assertEqual(
            {
                "error": 1,
                "expected-failure": 1,
                "failed": 1,
                "passed": 1,
                "skipped": 1,
                "unexpected-success": 1,
            },
            report["outcomes"],
        )

    def test_aggregates_modules_and_slowest_tests_deterministically(self):
        result = self._empty_timing_result()
        result.testsRun = 3
        result.timing_records = [
            {
                "id": "suite.test_b",
                "module": "suite.second",
                "outcome": "passed",
                "durationNs": 10_000_000,
            },
            {
                "id": "suite.test_a",
                "module": "suite.first",
                "outcome": "skipped",
                "durationNs": 10_000_000,
            },
            {
                "id": "suite.test_c",
                "module": "suite.first",
                "outcome": "expected-failure",
                "durationNs": 5_000_000,
            },
        ]

        report = build_timing_report(
            result,
            started_at="2026-08-28T00:00:00Z",
            completed_at="2026-08-28T00:00:00.025Z",
            duration_ns=25_000_000,
        )

        self.assertEqual(
            ["suite.test_a", "suite.test_b", "suite.test_c"],
            [entry["id"] for entry in report["slowest"]],
        )
        self.assertEqual(
            ["suite.first", "suite.second"],
            [entry["id"] for entry in report["modules"]],
        )
        self.assertEqual(15, report["modules"][0]["durationMs"])
        self.assertEqual(25, report["durationMs"])

    def test_rejects_incomplete_duplicate_or_unbounded_timing_records(self):
        result = self._empty_timing_result()
        result.testsRun = 1
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            build_timing_report(
                result,
                started_at="start",
                completed_at="complete",
                duration_ns=0,
            )

        result.testsRun = 2
        result.timing_records = [
            {
                "id": "suite.test_duplicate",
                "module": "suite",
                "outcome": "passed",
                "durationNs": 1,
            },
            {
                "id": "suite.test_duplicate",
                "module": "suite",
                "outcome": "passed",
                "durationNs": 2,
            },
        ]
        with self.assertRaisesRegex(RuntimeError, "duplicate test id"):
            build_timing_report(
                result,
                started_at="start",
                completed_at="complete",
                duration_ns=3,
            )

        result.testsRun = 1
        result.timing_records = [
            {
                "id": "x" * (MAX_TEST_IDENTIFIER_CHARACTERS + 1),
                "module": "suite",
                "outcome": "passed",
                "durationNs": 1,
            }
        ]
        with self.assertRaisesRegex(RuntimeError, "between 1 and"):
            build_timing_report(
                result,
                started_at="start",
                completed_at="complete",
                duration_ns=1,
            )

        result.timing_limit_exceeded = True
        with self.assertRaisesRegex(RuntimeError, "timed test count exceeds"):
            build_timing_report(
                result,
                started_at="start",
                completed_at="complete",
                duration_ns=1,
            )

    def test_stream_writer_completes_partial_writes_and_flushes(self):
        class PartialStream:
            def __init__(self):
                self.content = bytearray()
                self.flush_count = 0

            def write(self, content):
                count = min(7, len(content))
                self.content.extend(content[:count])
                return count

            def flush(self):
                self.flush_count += 1

        stream = PartialStream()
        report = {
            "schemaVersion": 1,
            "kind": "engineering-process-unittest-timing",
            "authority": "diagnostic-only",
            "tests": [],
        }

        write_timing_stream(stream, report)

        self.assertEqual(report, json.loads(bytes(stream.content)))
        self.assertEqual(1, stream.flush_count)

    def test_stream_writer_rejects_no_progress_failure_and_oversize(self):
        class NoProgressStream:
            def write(self, _content):
                return 0

            def flush(self):
                self.fail("flush must not be reached")

        with self.assertRaisesRegex(RuntimeError, "no valid write progress"):
            write_timing_stream(NoProgressStream(), {"status": "passed"})
        with self.assertRaisesRegex(RuntimeError, "cannot write"):
            write_timing_stream(mock.Mock(write=mock.Mock(side_effect=OSError("x"))), {})
        with self.assertRaisesRegex(RuntimeError, "exceeds"):
            write_timing_stream(
                io.BytesIO(),
                {"payload": "x" * (MAX_TIMING_REPORT_BYTES + 1)},
            )

    def test_descriptor_is_duplicated_and_coordinator_descriptor_stays_open(self):
        with tempfile.TemporaryFile(mode="w+b") as output:
            duplicate = duplicate_timing_descriptor(output.fileno())
            self.assertNotEqual(output.fileno(), duplicate)

            write_timing_descriptor(duplicate, {"status": "passed"})
            output.seek(0)
            self.assertEqual({"status": "passed"}, json.load(output))
            output.seek(0, os.SEEK_END)
            output.write(b"coordinator-still-open")

    def test_rejects_stdio_boolean_negative_and_closed_descriptors(self):
        for descriptor in (False, True, -1, 0, 1, 2):
            with self.subTest(descriptor=descriptor):
                with self.assertRaisesRegex(RuntimeError, "at least 3"):
                    duplicate_timing_descriptor(descriptor)

        read_descriptor, closed_descriptor = os.pipe()
        os.close(closed_descriptor)
        try:
            with self.assertRaisesRegex(RuntimeError, "cannot duplicate"):
                duplicate_timing_descriptor(closed_descriptor)
        finally:
            os.close(read_descriptor)

    def test_descriptor_duplicate_is_closed_when_configuration_fails(self):
        with tempfile.TemporaryFile(mode="w+b") as output:
            with (
                mock.patch(
                    "verification.run_test_suite.os.set_inheritable",
                    side_effect=OSError("fixture configuration failure"),
                ),
                mock.patch(
                    "verification.run_test_suite.os.close",
                    wraps=os.close,
                ) as close_descriptor,
            ):
                with self.assertRaisesRegex(RuntimeError, "cannot duplicate"):
                    duplicate_timing_descriptor(output.fileno())

        close_descriptor.assert_called_once()

    def test_keyboard_interrupt_remains_primary_when_duplicate_close_fails(self):
        class InterruptingStream:
            def write(self, _content):
                raise KeyboardInterrupt()

            def flush(self):
                raise AssertionError("flush must not be reached")

            def close(self):
                raise OSError("secondary close failure")

        with mock.patch(
            "verification.run_test_suite.os.fdopen",
            return_value=InterruptingStream(),
        ):
            with self.assertRaises(KeyboardInterrupt) as observed:
                write_timing_descriptor(99, {"status": "passed"})

        self.assertTrue(
            any("secondary close failure" in note for note in observed.exception.__notes__)
        )

    def test_duplicate_close_failure_is_actionable_after_complete_write(self):
        class CloseFailureStream:
            def write(self, content):
                return len(content)

            def flush(self):
                return None

            def close(self):
                raise OSError("fixture close failure")

        with mock.patch(
            "verification.run_test_suite.os.fdopen",
            return_value=CloseFailureStream(),
        ):
            with self.assertRaisesRegex(RuntimeError, "cannot close"):
                write_timing_descriptor(99, {"status": "passed"})

    def test_default_main_path_keeps_standard_runner_and_exit_semantics(self):
        suite = object()
        result = mock.Mock()
        result.wasSuccessful.return_value = False
        runner = mock.Mock()
        runner.run.return_value = result
        with (
            mock.patch(
                "verification.run_test_suite.configure_test_git_environment"
            ),
            mock.patch(
                "verification.run_test_suite.unittest.defaultTestLoader.discover",
                return_value=suite,
            ),
            mock.patch(
                "verification.run_test_suite.unittest.TextTestRunner",
                return_value=runner,
            ) as runner_type,
        ):
            exit_code = main([])

        self.assertEqual(1, exit_code)
        runner_type.assert_called_once_with(verbosity=1)
        runner.run.assert_called_once_with(suite)

    def test_timing_main_path_writes_to_duplicate_and_preserves_success(self):
        with tempfile.TemporaryFile(mode="w+b") as output:
            with (
                mock.patch(
                    "verification.run_test_suite.configure_test_git_environment"
                ),
                mock.patch(
                    "verification.run_test_suite.unittest.defaultTestLoader.discover",
                    return_value=self._passing_suite(),
                ),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = main(
                    ["--timing-output-fd", str(output.fileno())]
                )
            output.seek(0)
            report = json.load(output)

        self.assertEqual(0, exit_code)
        self.assertEqual("diagnostic-only", report["authority"])
        self.assertEqual("passed", report["status"])
        self.assertEqual(1, report["testCount"])
        self.assertNotIn("environment", report)
        self.assertNotIn("path", report)


if __name__ == "__main__":
    unittest.main()
