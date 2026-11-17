from ramen_runtime import image, resource, text


def _png() -> bytes:
    """A 1x1 red PNG built here, so the fixture carries no opaque blob."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    head = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", head)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


def run():
    return [
        text("Multiple content types test:"),
        image(_png(), "image/png"),
        resource("test://mixed-content-resource", text='{"test":"data","value":123}', mime="application/json"),
    ]
