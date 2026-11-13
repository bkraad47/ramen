import time

from ramen_runtime import progress, text


def run(steps: int):
    for n in range(1, int(steps) + 1):
        progress(n, total=int(steps), message=f"step {n}")
        time.sleep(0.02)
    return text(f"done {steps}")
