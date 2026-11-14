from ramen_runtime import InputRequired, elicit, input_responses, request_state, text

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}, "why": {"type": "string"}}, "required": ["ok"]}


def run(item: str):
    answer, state = input_responses().get("confirm"), request_state()
    if not isinstance(answer, dict):
        raise InputRequired({"confirm": elicit(f"Delete {item}?", SCHEMA)}, state={"item": item, "asked": 1})
    if state != {"item": item, "asked": 1}:
        return text(f"state lost: {state!r}")
    ok = answer.get("action") == "accept" and (answer.get("content") or {}).get("ok") is True
    return text(f"deleted {item}" if ok else f"kept {item}")
