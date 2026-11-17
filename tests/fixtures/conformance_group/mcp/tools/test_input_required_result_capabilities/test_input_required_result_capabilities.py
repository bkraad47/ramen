from ramen_runtime import InputRequired, MissingCapability, client_capabilities, elicit, input_responses, sample, text


def run():
    if input_responses():
        return text("input received")
    caps, requests = client_capabilities(), {}
    if "elicitation" in caps:
        requests["elicit_input"] = elicit(
            "What is your name?", {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        )
    if "sampling" in caps:
        requests["sample_input"] = sample("Say hello", max_tokens=50)
    if not requests:
        raise MissingCapability({"sampling": {}})
    raise InputRequired(requests, state={"kind": "capabilities-test"})
