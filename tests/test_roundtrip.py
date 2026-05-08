"""Round-trip tests for OmniComp v8 TURBO.

These tests exercise the full compress -> decompress -> equality cycle on
inputs designed to hit each niche: random, constant, numeric f64/f32/i64/i32,
text, structured (JSON-ish), low-cardinality floats and a heterogeneous mix.

Run with:
    python3 -m pytest tests/ -v
or directly:
    python3 tests/test_roundtrip.py
"""
from __future__ import annotations

import math
import os
import random
import struct
import sys
from pathlib import Path

# Make ``import omnicomp`` work when running this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnicomp import compress, decompress  # noqa: E402


def _assert_roundtrip(data: bytes, label: str) -> tuple[float, int, int]:
    comp = compress(data)
    back = decompress(comp)
    assert back == data, f"[{label}] round-trip mismatch ({len(data)} bytes)"
    ratio = len(comp) / max(1, len(data))
    return ratio, len(data), len(comp)


# ---------- Test cases ----------

def test_empty():
    assert decompress(compress(b"")) == b""


def test_single_byte():
    _assert_roundtrip(b"\x00", "single-byte")
    _assert_roundtrip(b"x", "single-byte-x")


def test_constant_block():
    data = b"A" * (300 * 1024)
    ratio, *_ = _assert_roundtrip(data, "constant")
    # Constant data should compress dramatically.
    assert ratio < 0.01, f"constant ratio too high: {ratio}"


def test_random_block():
    rng = random.Random(0xC0FFEE)
    data = bytes(rng.getrandbits(8) for _ in range(200 * 1024))
    ratio, *_ = _assert_roundtrip(data, "random")
    # High-entropy data should not blow up.
    assert ratio < 1.05, f"random ratio too high: {ratio}"


def test_text_block():
    text = (
        "The quick brown fox jumps over the lazy dog. "
        "Lossless compression preserves every bit. "
    )
    data = (text * 5000).encode("utf-8")
    ratio, *_ = _assert_roundtrip(data, "text")
    assert ratio < 0.20, f"text ratio too high: {ratio}"


def test_structured_block():
    rows = []
    for i in range(20000):
        rows.append('{"id": %d, "v": %d, "tag": "row-%d"}' % (i, i * 7, i % 16))
    data = ("[\n" + ",\n".join(rows) + "\n]").encode("utf-8")
    ratio, *_ = _assert_roundtrip(data, "structured")
    assert ratio < 0.20, f"structured ratio too high: {ratio}"


def test_numeric_f64_smooth():
    # Smooth quadratic: 2*x[i-1] - x[i-2] cancels exactly.
    n = 100_000
    data = struct.pack(f"<{n}d", *(0.001 * i * i for i in range(n)))
    ratio, *_ = _assert_roundtrip(data, "numeric_f64_smooth")
    assert ratio < 0.20, f"f64 smooth ratio too high: {ratio}"


def test_numeric_f64_sine():
    n = 100_000
    data = struct.pack(f"<{n}d", *(math.sin(0.001 * i) for i in range(n)))
    ratio, *_ = _assert_roundtrip(data, "numeric_f64_sine")
    # Trig values keep noisy low-order mantissa bits, so the ratio is
    # not as aggressive as for a smooth quadratic. The hard requirement
    # here is exact round-trip; we still want some compression though.
    assert ratio < 0.75, f"f64 sine ratio too high: {ratio}"


def test_numeric_f32():
    n = 200_000
    data = struct.pack(f"<{n}f", *(0.01 * i for i in range(n)))
    ratio, *_ = _assert_roundtrip(data, "numeric_f32")
    assert ratio < 0.30, f"f32 ratio too high: {ratio}"


def test_numeric_i32_ids():
    n = 200_000
    data = struct.pack(f"<{n}i", *range(n))
    ratio, *_ = _assert_roundtrip(data, "numeric_i32_ids")
    assert ratio < 0.20, f"i32 IDs ratio too high: {ratio}"


def test_numeric_i64_timestamps():
    n = 100_000
    base = 1_700_000_000
    data = struct.pack(f"<{n}q", *(base + i * 1000 for i in range(n)))
    ratio, *_ = _assert_roundtrip(data, "numeric_i64_timestamps")
    assert ratio < 0.20, f"i64 timestamps ratio too high: {ratio}"


def test_numeric_i16_pcm():
    n = 200_000
    samples = [int(20000 * math.sin(0.05 * i)) for i in range(n)]
    data = struct.pack(f"<{n}h", *samples)
    ratio, *_ = _assert_roundtrip(data, "numeric_i16_pcm")
    assert ratio < 0.95, f"i16 PCM ratio too high: {ratio}"


def test_low_cardinality_floats():
    n = 80_000
    rng = random.Random(0xBEEF)
    palette = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0]
    data = struct.pack(f"<{n}d", *(rng.choice(palette) for _ in range(n)))
    ratio, *_ = _assert_roundtrip(data, "low_cardinality")
    assert ratio < 0.20, f"low-cardinality ratio too high: {ratio}"


def test_heterogeneous_mix():
    # Mix of text + numeric + random + constant in one stream to exercise
    # the multi-block dispatcher.
    parts = []
    parts.append(("Hello, world! " * 4000).encode("utf-8"))
    parts.append(struct.pack("<10000d", *(0.001 * i * i for i in range(10000))))
    rng = random.Random(7)
    parts.append(bytes(rng.getrandbits(8) for _ in range(50_000)))
    parts.append(b"Z" * 50_000)
    data = b"".join(parts)
    _assert_roundtrip(data, "heterogeneous")


def test_random_binary_sweep():
    """Many small inputs of varied sizes to surface boundary bugs."""
    rng = random.Random(1234)
    for size in [0, 1, 63, 64, 100, 4095, 4096, 65535, 256 * 1024 + 17]:
        data = bytes(rng.getrandbits(8) for _ in range(size))
        back = decompress(compress(data))
        assert back == data, f"sweep size={size} mismatch"


# ---------- Manual runner ----------

def main():
    tests = [
        ("empty",                 test_empty),
        ("single byte",           test_single_byte),
        ("constant block",        test_constant_block),
        ("random block",          test_random_block),
        ("text block",            test_text_block),
        ("structured block",      test_structured_block),
        ("numeric f64 smooth",    test_numeric_f64_smooth),
        ("numeric f64 sine",      test_numeric_f64_sine),
        ("numeric f32",           test_numeric_f32),
        ("numeric i32 ids",       test_numeric_i32_ids),
        ("numeric i64 timestamps",test_numeric_i64_timestamps),
        ("numeric i16 PCM",       test_numeric_i16_pcm),
        ("low-cardinality f64",   test_low_cardinality_floats),
        ("heterogeneous mix",     test_heterogeneous_mix),
        ("random binary sweep",   test_random_binary_sweep),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print()
    if failed:
        print(f"{failed} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
