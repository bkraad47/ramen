import time

from ramen_runtime import progress, text


def run():
    for done in (0, 50, 100):
        progress(done, total=100, message=f"Completed {done} of 100")
        time.sleep(0.05)
    return text("Progress complete")
