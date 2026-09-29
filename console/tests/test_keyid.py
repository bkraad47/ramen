"""The console must reproduce the worker's `key_id` so the Logs page can name the consumer (CONTRACTS §12.1 U4).

`key_id` is node-rs `auth::key_id`: Rust `DefaultHasher` (SipHash-1-3, k0=k1=0) over the key's UTF-8 bytes plus
the 0xff terminator `Hasher::write_str` appends, truncated to u32 and printed `{:08x}`. The vectors below were
produced by compiling that exact function with the toolchain pinned in `rust-toolchain.toml` (rustc 1.98.1);
if Rust ever changes `DefaultHasher`, this test fails and tells us the mapping needs revisiting.
"""

from ramen_console.keyid import key_id

RUST_VECTORS = {
    "": "23c53def",
    "a": "a4f0e9f3",
    "k1": "776c599b",
    "k2": "2364fd8e",
    "rmk_abcdef": "6dffe2e6",
    "rmk_0123456789012345678901234567890123": "51b52285",
    "hello world": "98bec7cf",
    "ééé": "44fa4a5a",
}


def test_matches_the_rust_node():
    assert {k: key_id(k) for k in RUST_VECTORS} == RUST_VECTORS


def test_is_eight_lowercase_hex_digits():
    for k in ("rmk_" + "x" * 40, "short", ""):
        assert len(key_id(k)) == 8
        assert all(c in "0123456789abcdef" for c in key_id(k))
