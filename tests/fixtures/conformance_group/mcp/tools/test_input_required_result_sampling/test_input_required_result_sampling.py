from ramen_runtime import InputRequired, input_responses, sample, text


def run():
    answer = input_responses().get("sample_request")
    if not isinstance(answer, dict):
        raise InputRequired({"sample_request": sample("What is the capital of France?", max_tokens=100)})
    return text(f"Model said: {(answer.get('content') or {}).get('text', '')}")
