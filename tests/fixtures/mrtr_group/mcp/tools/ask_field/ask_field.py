from ramen_runtime import InputRequired, elicit, input_responses, text


def run(field: str):
    if "login" not in input_responses():
        schema = {"type": "object", "properties": {field: {"type": "string"}}, "required": [field]}
        raise InputRequired({"login": elicit("Fill in the form", schema)})
    return text("got it")
