"""JSON-lines logging on stderr. Never pass secret values as fields."""
import json
import sys
import time


def log(level: str, msg: str, **fields) -> None:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "level": level, "msg": msg, **fields}
    sys.stderr.write(json.dumps(rec, default=str) + "\n")
    sys.stderr.flush()
