import time

from ramen_runtime import progress, text


def run(seconds: int, label: str):
    total = max(int(seconds), 0) * 10
    for n in range(total):
        progress(n, total=total)
        time.sleep(0.1)
    return text(f"job {label} done")
