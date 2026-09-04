#!/usr/bin/env python3
"""Wait until consumers cannot reuse a valid pre-release PyPI Simple response."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import time
from urllib.request import Request, urlopen


SIMPLE_MEDIA_TYPE = "application/vnd.pypi.simple.v1+json"
MAX_SIMPLE_CACHE_SECONDS = 900
CLOCK_MARGIN_SECONDS = 5
MAX_FUTURE_SKEW_SECONDS = 60


def parse_max_age(values: list[str] | None) -> int:
    """Return the sole bounded max-age across every Cache-Control field."""
    if not values:
        raise RuntimeError("PyPI Simple API did not declare Cache-Control")
    max_ages: list[int] = []
    for field in values:
        for directive in field.split(","):
            name, separator, value = directive.strip().partition("=")
            if name.lower() != "max-age":
                continue
            if separator != "=" or not value.isdecimal():
                raise RuntimeError("PyPI Simple API declared an invalid max-age")
            max_ages.append(int(value))
    if len(max_ages) != 1:
        raise RuntimeError("PyPI Simple API must declare exactly one max-age")
    if max_ages[0] > MAX_SIMPLE_CACHE_SECONDS:
        raise RuntimeError(
            f"PyPI Simple API max-age exceeds {MAX_SIMPLE_CACHE_SECONDS} seconds"
        )
    return max_ages[0]


def remaining_seconds(published_at: str, max_age: int, *, now: datetime) -> float:
    """Calculate the bounded remaining horizon from a post-visibility event."""
    normalized = published_at[:-1] + "+00:00" if published_at.endswith("Z") else published_at
    try:
        published = datetime.fromisoformat(normalized)
    except (TypeError, ValueError) as error:
        raise RuntimeError("GitHub release has an invalid publication timestamp") from error
    if published.tzinfo is None or now.tzinfo is None:
        raise RuntimeError("cache horizon timestamps must include timezones")
    published = published.astimezone(timezone.utc)
    now = now.astimezone(timezone.utc)
    if (published - now).total_seconds() > MAX_FUTURE_SKEW_SECONDS:
        raise RuntimeError("GitHub release publication timestamp is too far in the future")
    deadline = published + timedelta(seconds=max_age + CLOCK_MARGIN_SECONDS)
    return max(0.0, (deadline - now).total_seconds())


def read_simple_max_age() -> int:
    request = Request(
        "https://pypi.org/simple/engineering-process/",
        headers={"Accept": SIMPLE_MEDIA_TYPE},
    )
    with urlopen(request, timeout=20) as response:
        if response.headers.get_content_type() != SIMPLE_MEDIA_TYPE:
            raise RuntimeError("PyPI Simple API did not return PEP 691 JSON")
        values = response.headers.get_all("Cache-Control")
    return parse_max_age(values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_published_at")
    args = parser.parse_args(argv)
    remaining = remaining_seconds(
        args.release_published_at,
        read_simple_max_age(),
        now=datetime.now(timezone.utc),
    )
    if remaining:
        print(f"Waiting {remaining:.0f} seconds for consumer Simple caches")
        time.sleep(remaining)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
