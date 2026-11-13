from ramen_runtime import InputRequired, elicit, input_responses, text


def _content(response) -> dict | None:
    """The `content` of an accepted elicitation answer, else None."""
    if isinstance(response, dict) and response.get("action") == "accept" and isinstance(response.get("content"), dict):
        return response["content"]
    return None


def run():
    answer = _content(input_responses().get("user_name"))
    if answer is None:
        raise InputRequired(
            {
                "user_name": elicit(
                    "What is your name?",
                    {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
                )
            }
        )
    return text(f"Hello, {answer.get('name')}!")
