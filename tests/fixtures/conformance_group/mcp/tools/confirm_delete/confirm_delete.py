from ramen_runtime import InputRequired, elicit, input_responses, text

SCHEMA = {"type": "object", "properties": {"confirm": {"type": "boolean"}}, "required": ["confirm"]}


def run(filename: str):
    answer = input_responses().get("confirm")
    if not isinstance(answer, dict):
        raise InputRequired({"confirm": elicit(f"Delete {filename}?", SCHEMA)})
    confirmed = answer.get("action") == "accept" and (answer.get("content") or {}).get("confirm") is not False
    return text(f"deleted {filename}" if confirmed else f"kept {filename}")
