import json


def read(id: str) -> str:  # noqa: A002 - the template variable is named by the official suite
    return json.dumps({"id": id, "templateTest": True, "data": f"Data for ID: {id}"}, separators=(",", ":"))
