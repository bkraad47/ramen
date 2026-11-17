from ramen_runtime import resource, text


def build(resourceUri: str) -> list:  # noqa: N803 - the argument is named by the official suite
    return [
        {
            "role": "user",
            "content": resource(resourceUri, text="Embedded resource content for testing.", mime="text/plain"),
        },
        {"role": "user", "content": text("Please process the embedded resource above.")},
    ]
