"""Demo calculator tool for Ramen. `utils/` is on sys.path at runtime."""
import calculator_utils as cu


def calculator_func(var1: float, var2: float, func: str) -> float:
    ops = {"add": cu.add, "subtract": cu.sub, "multiply": cu.mul, "divide": cu.div}
    if func not in ops:
        raise ValueError(f"unknown func {func!r}; expected one of {sorted(ops)}")
    return ops[func](var1, var2)
