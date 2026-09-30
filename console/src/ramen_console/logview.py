"""Turn a worker's log text into the entries the two-pane Logs page renders (U4, CONTRACTS §12.1).

A worker writes one JSON object per line: `ts, level, msg, ip, group, zone, env, method, name, status,
grpc_code, ms, key_id` (node-rs `log::emit`). Anything else — sidecar output, startup banners, a truncated
line — is kept verbatim so nothing is silently dropped.
"""

import json

ANONYMOUS = "anonymous"


def _entry(i: int, raw: str, names: dict[str, str]) -> dict:
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            raise ValueError
    except ValueError, TypeError:
        return {
            "i": i,
            "raw": raw,
            "ts": "",
            "key_id": None,
            "consumer": "",
            "method": None,
            "name": None,
            "status": "",
            "outcome": "unknown",
            "body": raw,
            "selected": False,
        }
    kid = doc.get("key_id")
    status = doc.get("status") or ""
    call = bool(status)  # only a worker's per-call line carries `status`; startup and sidecar lines do not
    return {
        "i": i,
        "raw": raw,
        "ts": doc.get("ts") or "",
        "key_id": kid,
        "consumer": (names.get(kid) or kid or ANONYMOUS) if call else (doc.get("msg") or ""),
        "method": doc.get("method"),
        "name": doc.get("name"),
        "status": status,
        "outcome": ("success" if status == "ok" else "failure") if call else "unknown",
        "body": json.dumps(doc, indent=2),
        "selected": False,
    }


def parse_log(text: str | None, names: dict[str, str] | None = None) -> list[dict]:
    """Newest first, the newest one selected. `names` maps a worker's `key_id` to the key name."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    entries = [_entry(i, ln, names or {}) for i, ln in enumerate(reversed(lines))]
    if entries:
        entries[0]["selected"] = True
    return entries
