"""B3 / C5 (0.7.0): is a canary's manifest compatible with the stable track's? Pure functions, no I/O.

Breaking: a removed tool/resource/prompt, a removed property, a new required property (or prompt argument), a changed
`type`, a narrowed `enum`. Everything else that adds is additive; descriptions and `_meta` never matter.
"""

import hashlib
import json

KINDS = (("tools", "tool", "name"), ("resources", "resource", "uri"), ("prompts", "prompt", "name"))


def manifest_hash(result: dict) -> str:
    """C1: sha256 of the canonical JSON of tools+resources+prompts, prompts without `_meta`."""
    body = {
        "tools": result.get("tools") or [],
        "resources": result.get("resources") or [],
        "prompts": [{k: v for k, v in p.items() if k != "_meta"} for p in result.get("prompts") or []],
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def manifest_of(result) -> dict | None:
    """C3: `{"hash", "tools", "resources", "prompts"}` from a reload result; None when it reported no manifest."""
    if not isinstance(result, dict) or not any(k in result for k in ("tools", "resources", "prompts")):
        return None
    m = {
        "hash": result.get("hash") or manifest_hash(result),
        **{k: list(result.get(k) or []) for k in ("tools", "resources", "prompts")},
    }
    if isinstance(result.get("guardrails"), dict):  # §21.2: shown per zone, never part of the hash or the diff
        m["guardrails"] = result["guardrails"]
    return m


def diff(old: dict | None, new: dict | None) -> dict:
    """`{"breaking": [...], "additive": [...]}`, one plain sentence per change. No stored manifest: nothing breaks."""
    out = {"breaking": [], "additive": []}
    if old is None or new is None:
        return out
    for key, label, ident in KINDS:
        before = {x.get(ident): x for x in old.get(key) or []}
        after = {x.get(ident): x for x in new.get(key) or []}
        for n in before:
            if n not in after:
                out["breaking"].append(f"removed {label} {n}")
        for n in after:
            if n not in before:
                out["additive"].append(f"added {label} {n}")
        for n in before.keys() & after.keys():
            if key == "tools":
                _tool(f"tool {n}", before[n], after[n], out)
            elif key == "prompts":
                _prompt(f"prompt {n}", before[n], after[n], out)
    return out


def is_breaking(d: dict) -> bool:
    return bool(d["breaking"])


def summary(d: dict) -> str:
    parts = [f"{k}: {'; '.join(v)}" for k, v in (("breaking", d["breaking"]), ("additive", d["additive"])) if v]
    return "; ".join(parts) or "no schema changes"


def _tool(where, before, after, out):
    _schema(where, before.get("inputSchema") or {}, after.get("inputSchema") or {}, out, "property ")
    ob, oa = before.get("outputSchema"), after.get("outputSchema")
    if ob and not oa:
        out["breaking"].append(f"{where}: output schema removed")
    elif oa and not ob:
        out["additive"].append(f"{where}: output schema added")
    elif ob and oa:
        _schema(where, ob, oa, out, "output property ")


def _prompt(where, before, after, out):
    b = {a["name"]: a for a in before.get("arguments") or []}
    a = {x["name"]: x for x in after.get("arguments") or []}
    for n in b:
        if n not in a:
            out["breaking"].append(f"{where}: removed argument {n}")
    for n, arg in a.items():
        if n not in b:
            kind = "required" if arg.get("required") else "optional"
            out[("breaking" if kind == "required" else "additive")].append(f"{where}: new {kind} argument {n}")
        elif arg.get("required") and not b[n].get("required"):
            out["breaking"].append(f"{where}: argument {n} is now required")


def _types(s) -> list[str]:
    t = s.get("type")
    return sorted(t) if isinstance(t, list) else ([t] if t else [])


def _schema(where, old, new, out, prefix, path=""):
    ob, oa = old.get("properties") or {}, new.get("properties") or {}
    rb, ra = set(old.get("required") or []), set(new.get("required") or [])
    for n in ob:
        if n not in oa:
            out["breaking"].append(f"{where}: removed {prefix}{path}{n}")
    for n in oa:
        if n not in ob:
            kind = "required" if n in ra else "optional"
            out["breaking" if kind == "required" else "additive"].append(f"{where}: new {kind} {prefix}{path}{n}")
        else:
            if n in ra and n not in rb:
                out["breaking"].append(f"{where}: {prefix}{path}{n} is now required")
            elif n in rb and n not in ra:
                out["additive"].append(f"{where}: {prefix}{path}{n} no longer required")
            _value(where, ob[n], oa[n], out, prefix, f"{path}{n}")


def _value(where, old, new, out, prefix, path):
    tb, ta = _types(old), _types(new)
    if tb and ta and tb != ta:
        out["breaking"].append(f"{where}: {prefix}{path} type {', '.join(tb)} -> {', '.join(ta)}")
    eb, ea = old.get("enum"), new.get("enum")
    if ea is not None and eb is None:
        out["breaking"].append(f"{where}: {prefix}{path} enum narrowed, removed any value")
    elif eb is not None and ea is not None:
        gone = [str(v) for v in eb if v not in ea]
        new_vals = [str(v) for v in ea if v not in eb]
        if gone:
            out["breaking"].append(f"{where}: {prefix}{path} enum narrowed, removed {', '.join(gone)}")
        elif new_vals:
            out["additive"].append(f"{where}: {prefix}{path} enum widened, added {', '.join(new_vals)}")
    elif eb is not None and ea is None:
        out["additive"].append(f"{where}: {prefix}{path} enum removed")
    if old.get("properties") or new.get("properties"):
        _schema(where, old, new, out, prefix, f"{path}.")
    if isinstance(old.get("items"), dict) and isinstance(new.get("items"), dict):
        _value(where, old["items"], new["items"], out, prefix, f"{path}[]")
