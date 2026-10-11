"""Exact counts for a text; the worked example of docs/wiki/mcp-repo.md."""


def count(text: str) -> dict:
    return {"characters": len(text), "words": len(text.split()), "lines": len(text.splitlines())}
