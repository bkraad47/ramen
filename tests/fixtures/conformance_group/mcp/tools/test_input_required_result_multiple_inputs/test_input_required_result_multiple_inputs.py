from ramen_runtime import InputRequired, elicit, input_responses, list_roots, request_state, sample, text

KEYS = ("user_name", "greeting", "client_roots")


def run():
    answers, state = input_responses(), request_state()
    if not isinstance(state, dict) or state.get("kind") != "multiple-inputs" or any(k not in answers for k in KEYS):
        raise InputRequired(
            {
                "user_name": elicit(
                    "What is your name?",
                    {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
                ),
                "greeting": sample("Generate a greeting", max_tokens=50),
                "client_roots": list_roots(),
            },
            state={"kind": "multiple-inputs"},
        )
    return text("all three inputs received")
