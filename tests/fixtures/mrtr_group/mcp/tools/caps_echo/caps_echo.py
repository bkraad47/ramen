import json

from ramen_runtime import client_capabilities, text


def run():
    return text(json.dumps(client_capabilities(), sort_keys=True))
