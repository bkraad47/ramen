import time

from ramen_runtime import text


def run(seconds: int, label: str):
    deadline = time.monotonic() + min(max(int(seconds), 0), 120)
    while time.monotonic() < deadline:
        time.sleep(0.05)
    return text(f"computed {label} in {seconds}s")
