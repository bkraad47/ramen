from ramen_runtime import InputRequired, elicit, input_responses

SCHEMA = {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}


def build() -> str:
    answer = input_responses().get("topic")
    if not isinstance(answer, dict):
        raise InputRequired({"topic": elicit("Which topic?", SCHEMA)})
    return f"Write about {(answer.get('content') or {}).get('topic')}."
