from ramen_runtime import resource


def run():
    return resource("test://embedded-resource", text="This is an embedded resource content.", mime="text/plain")
