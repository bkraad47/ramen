from ramen_runtime import InputRequired, input_responses, sample, text


def run():
    answer = input_responses().get("llm")
    if not isinstance(answer, dict):
        raise InputRequired({"llm": sample("Name a noodle", max_tokens=20)})
    return text((answer.get("content") or {}).get("text", ""))
