from ramen_runtime import InputRequired, elicit, input_responses, text


def run():
    if "confirm" not in input_responses():
        raise InputRequired(
            {
                "confirm": elicit(
                    "Please confirm", {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
                )
            }
        )
    return text("confirmed")
