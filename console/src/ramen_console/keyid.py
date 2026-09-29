"""Reproduce node-rs `auth::key_id` so the console can name the consumer behind a worker log line (§12.1 U4).

The node logs `format!("{:08x}", DefaultHasher(key).finish() as u32)`. Rust's `DefaultHasher` is SipHash-1-3
keyed with zeros, and hashing a `str` writes its bytes followed by the 0xff terminator. Pinned by
`tests/test_keyid.py` against vectors taken from the real Rust function.
"""

_M = (1 << 64) - 1


def _rotl(x: int, b: int) -> int:
    return ((x << b) | (x >> (64 - b))) & _M


def _round(v0: int, v1: int, v2: int, v3: int) -> tuple[int, int, int, int]:
    v0 = (v0 + v1) & _M
    v1 = _rotl(v1, 13) ^ v0
    v0 = _rotl(v0, 32)
    v2 = (v2 + v3) & _M
    v3 = _rotl(v3, 16) ^ v2
    v0 = (v0 + v3) & _M
    v3 = _rotl(v3, 21) ^ v0
    v2 = (v2 + v1) & _M
    v1 = _rotl(v1, 17) ^ v2
    return v0, v1, _rotl(v2, 32), v3


def siphash13(data: bytes) -> int:
    """SipHash-1-3 with a zero key, i.e. Rust's `DefaultHasher`."""
    v0, v1, v2, v3 = 0x736F6D6570736575, 0x646F72616E646F6D, 0x6C7967656E657261, 0x7465646279746573
    n = len(data)
    full = n - n % 8
    for i in range(0, full, 8):
        m = int.from_bytes(data[i : i + 8], "little")
        v3 ^= m
        v0, v1, v2, v3 = _round(v0, v1, v2, v3)
        v0 ^= m
    tail = int.from_bytes(data[full:].ljust(7, b"\0"), "little") | ((n & 0xFF) << 56)
    v3 ^= tail
    v0, v1, v2, v3 = _round(v0, v1, v2, v3)
    v0 ^= tail
    v2 ^= 0xFF
    for _ in range(3):
        v0, v1, v2, v3 = _round(v0, v1, v2, v3)
    return v0 ^ v1 ^ v2 ^ v3


def key_id(key: str) -> str:
    """The non-secret 8-hex-digit id the worker logs for an MCP key."""
    return format(siphash13(key.encode() + b"\xff") & 0xFFFFFFFF, "08x")
