from ramen_runtime import InputRequired, elicit, input_responses, request_state, text


def _content(response) -> dict | None:
    """The `content` of an accepted elicitation answer, else None."""
    if isinstance(response, dict) and response.get("action") == "accept" and isinstance(response.get("content"), dict):
        return response["content"]
    return None


COLOR = {"type": "object", "properties": {"color": {"type": "string"}}, "required": ["color"]}


def run():
    answers, state = input_responses(), request_state()
    state = state if isinstance(state, dict) else {}
    if state.get("round") == 2 and (two := _content(answers.get("step2"))) is not None:
        return text(f"{state.get('name')} likes {two.get('color')}")
    if state.get("round") == 1 and (one := _content(answers.get("step1"))) is not None:
        raise InputRequired(
            {"step2": elicit("Step 2: What is your favorite color?", COLOR)},
            state={"round": 2, "name": one.get("name")},
        )
    raise InputRequired(
        {
            "step1": elicit(
                "Step 1: What is your name?",
                {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            )
        },
        state={"round": 1},
    )
