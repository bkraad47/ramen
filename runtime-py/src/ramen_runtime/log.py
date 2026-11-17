"""JSON-lines logging on stderr. Never pass secret values as fields."""

import json
import sys
import threading
import time

_LOCK = threading.Lock()  # task threads log too (§23.5)


def log(level: str, msg: str, **fields) -> None:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "level": level, "msg": msg, **fields}
    with _LOCK:
        sys.stderr.write(json.dumps(rec, default=str) + "\n")
        sys.stderr.flush()
