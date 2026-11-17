import json

from ramen_runtime import text


def run(region: str, count: int, flag: bool, mode: str, note: str):
    return text(
        json.dumps({"region": region, "count": count, "flag": flag, "mode": mode, "note": note}, sort_keys=True)
    )
