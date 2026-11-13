from ramen_runtime import InputRequired, input_responses, list_roots, text


def run():
    answer = input_responses().get("roots")
    if not isinstance(answer, dict):
        raise InputRequired({"roots": list_roots()})
    return text(",".join(r.get("uri", "") for r in answer.get("roots") or []))
