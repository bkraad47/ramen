"""A tool whose output honours its schema only for digit input (tests/fixtures/schema_group)."""


def run(a: str) -> dict:
    return {"n": int(a)} if str(a).isdigit() else {"n": a}
