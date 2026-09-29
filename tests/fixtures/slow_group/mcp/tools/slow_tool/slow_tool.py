"""A tool that takes as long as it is told to (tests/fixtures/slow_group)."""

import time


def slow(ms: int) -> str:
    time.sleep(min(int(ms), 10_000) / 1000)
    return f"slept {ms} ms"
