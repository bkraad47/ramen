from ramen_runtime import InputRequired, elicit, input_responses, text

SCHEMA = {
    "type": "object",
    "properties": {
        "username": {"type": "string", "description": "User's response"},
        "email": {"type": "string", "description": "User's email address"},
    },
    "required": ["username", "email"],
}


def run(message: str):
    answer = input_responses().get("user_info")
    if not isinstance(answer, dict):
        raise InputRequired({"user_info": elicit(message, SCHEMA)})
    return text(f"User response: action={answer.get('action')}, content={answer.get('content')}")
