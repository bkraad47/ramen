from ramen_runtime import InputRequired, elicit, input_responses

SCHEMA = {"type": "object", "properties": {"context": {"type": "string"}}, "required": ["context"]}


def build() -> str:
    answer = input_responses().get("user_context")
    if not isinstance(answer, dict):
        raise InputRequired({"user_context": elicit("What context should the prompt use?", SCHEMA)})
    return f"Prompt with context: {(answer.get('content') or {}).get('context')}"
