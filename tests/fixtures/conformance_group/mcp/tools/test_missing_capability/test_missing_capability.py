from ramen_runtime import MissingCapability, client_capabilities, text


def run():
    if "sampling" not in client_capabilities():
        raise MissingCapability({"sampling": {}})
    return text("Success")
