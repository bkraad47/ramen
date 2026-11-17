from ramen_runtime import InputRequired, elicit, input_responses, text

ASK = {
    "first": (
        "What is your first name?",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    ),
    "second": (
        "What is your last name?",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    ),
}


def run():
    answers = input_responses()
    missing = {key: elicit(*ask) for key, ask in ASK.items() if key not in answers}
    if missing:
        raise InputRequired(missing)
    return text("both answers received")
