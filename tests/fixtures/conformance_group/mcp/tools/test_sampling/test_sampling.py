from ramen_runtime import InputRequired, input_responses, sample, text


def run(prompt: str):
    answer = input_responses().get("sample")
    if not isinstance(answer, dict):
        raise InputRequired({"sample": sample(prompt, max_tokens=100)})
    return text(f"LLM response: {(answer.get('content') or {}).get('text', '')}")
