import time


def run():
    time.sleep(1)
    raise RuntimeError("the job failed")
