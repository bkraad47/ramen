from ramen_runtime import InputRequired, elicit, input_responses, text

SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}


def run():
    answer = input_responses().get("who")
    if not isinstance(answer, dict):
        raise InputRequired({"who": elicit("What is your name?", SCHEMA)})
    return text(f"Hello, {(answer.get('content') or {}).get('name')}!")
