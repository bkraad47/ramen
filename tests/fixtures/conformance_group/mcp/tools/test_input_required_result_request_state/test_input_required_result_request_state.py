from ramen_runtime import InputRequired, elicit, input_responses, request_state, text


def _content(response) -> dict | None:
    """The `content` of an accepted elicitation answer, else None."""
    if isinstance(response, dict) and response.get("action") == "accept" and isinstance(response.get("content"), dict):
        return response["content"]
    return None


def run():
    state, answer = request_state(), _content(input_responses().get("confirm"))
    if not isinstance(state, dict) or state.get("kind") != "request-state" or answer is None:
        raise InputRequired(
            {
                "confirm": elicit(
                    "Please confirm", {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
                )
            },
            state={"kind": "request-state"},
        )
    if answer.get("ok") is not True:
        return text("not confirmed")
    return text("state-ok: requestState validated")
