from ramen_runtime import InputRequired, input_responses, list_roots, text


def run():
    answer = input_responses().get("roots_request")
    if not isinstance(answer, dict):
        raise InputRequired({"roots_request": list_roots()})
    return text(f"Client has {len(answer.get('roots') or [])} root(s)")
