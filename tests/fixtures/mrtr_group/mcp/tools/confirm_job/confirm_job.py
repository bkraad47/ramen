from ramen_runtime import InputRequired, elicit, input_responses, text

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}, "why": {"type": "string"}}, "required": ["ok"]}


def run(item: str):
    answer = input_responses().get("confirm")
    if not isinstance(answer, dict):
        raise InputRequired({"confirm": elicit(f"Delete {item}?", SCHEMA)})
    ok = answer.get("action") == "accept" and (answer.get("content") or {}).get("ok") is True
    return text(f"deleted {item}" if ok else f"kept {item}")
