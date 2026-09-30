"""Fixture tool for I13: fails at call time unless the group's requirements.txt was really pip-installed."""

import numpy as np


def stats_func(values: str) -> dict:
    arr = np.array([float(v) for v in values.split(",")], dtype=float)
    return {"mean": float(np.mean(arr)), "std": float(np.std(arr)), "numpy_version": np.__version__}
