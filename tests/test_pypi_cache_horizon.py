from datetime import datetime, timezone
from email.message import Message
import unittest
from unittest.mock import patch

from verification.wait_for_pypi_cache_horizon import (
    MAX_SIMPLE_CACHE_SECONDS,
    read_simple_max_age,
    parse_max_age,
    remaining_seconds,
)


class _Response:
    def __init__(self, headers: Message) -> None:
        self.headers = headers

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class PyPICacheHorizonTests(unittest.TestCase):
    NOW = datetime(2026, 9, 4, 13, 30, tzinfo=timezone.utc)

    def test_parses_current_bounded_header(self) -> None:
        self.assertEqual(600, parse_max_age(["max-age=600, public"]))
        self.assertEqual(
            MAX_SIMPLE_CACHE_SECONDS,
            parse_max_age([f"public, max-age={MAX_SIMPLE_CACHE_SECONDS}"]),
        )

    def test_rejects_missing_duplicate_malformed_and_excessive_max_age(self) -> None:
        invalid = [
            None,
            ["public"],
            ["max-age=600", "max-age=300"],
            ["max-age=-1"],
            ["max-age=invalid"],
            [f"max-age={MAX_SIMPLE_CACHE_SECONDS + 1}"],
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(RuntimeError):
                parse_max_age(values)

    def test_reads_every_cache_control_field(self) -> None:
        headers = Message()
        headers["Content-Type"] = "application/vnd.pypi.simple.v1+json"
        headers["Cache-Control"] = "public, max-age=600"
        headers["Cache-Control"] = "max-age=300"
        with patch(
            "verification.wait_for_pypi_cache_horizon.urlopen",
            return_value=_Response(headers),
        ), self.assertRaises(RuntimeError):
            read_simple_max_age()

    def test_current_horizon_and_elapsed_resume(self) -> None:
        self.assertEqual(
            305,
            remaining_seconds("2026-09-04T13:20:00Z", 900, now=self.NOW),
        )
        self.assertEqual(
            0,
            remaining_seconds("2026-09-04T13:00:00Z", 600, now=self.NOW),
        )

    def test_rejects_malformed_naive_and_future_timestamps(self) -> None:
        invalid = [
            "invalid",
            "2026-09-04T13:20:00",
            "2026-09-04T13:31:01Z",
        ]
        for published_at in invalid:
            with self.subTest(published_at=published_at), self.assertRaises(RuntimeError):
                remaining_seconds(published_at, 600, now=self.NOW)


if __name__ == "__main__":
    unittest.main()
