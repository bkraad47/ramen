"""Content blocks a tool or callable prompt may return (CONTRACTS §23.3)."""

import base64


class Block(dict):
    """Marks a dict as MCP content. Only builder output is passed through as content: a tool that returns a plain
    dict keeps its 0.8 answer (the JSON text of that dict)."""


def text(s) -> Block:
    return Block(type="text", text=str(s))


def image(data: bytes, mime: str) -> Block:
    return Block(type="image", data=b64(data), mimeType=mime)


def audio(data: bytes, mime: str) -> Block:
    return Block(type="audio", data=b64(data), mimeType=mime)


def resource(uri: str, text: str | None = None, blob: bytes | None = None, mime: str = "text/plain") -> Block:
    if (text is None) == (blob is None):
        raise ValueError("resource() takes exactly one of text or blob")
    body = {"text": text} if text is not None else {"blob": b64(blob)}
    return Block(type="resource", resource={"uri": uri, "mimeType": mime} | body)


def b64(data: bytes) -> str:
    return base64.b64encode(bytes(data)).decode("ascii")


def blocks(value) -> list[Block] | None:
    """`value` as a content list when it is one block or a non-empty list of blocks, else None (a JSON value)."""
    if isinstance(value, Block):
        return [value]
    if isinstance(value, list) and value and all(isinstance(v, Block) for v in value):
        return value
    return None
