"""Quick benchmark: ratio + throughput on synthetic inputs.

Usage:
    python3 examples/benchmark.py
"""
from __future__ import annotations

import math
import random
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnicomp import compress, decompress


def make_text(n_bytes: int) -> bytes:
    line = "The quick brown fox jumps over the lazy dog. "
    out = (line * (n_bytes // len(line) + 1)).encode("utf-8")
    return out[:n_bytes]


def make_numeric_f64(n_bytes: int) -> bytes:
    n = n_bytes // 8
    return struct.pack(f"<{n}d", *(0.001 * i * i for i in range(n)))


def make_numeric_i32(n_bytes: int) -> bytes:
    n = n_bytes // 4
    return struct.pack(f"<{n}i", *range(n))


def make_random(n_bytes: int) -> bytes:
    rng = random.Random(0xC0FFEE)
    return bytes(rng.getrandbits(8) for _ in range(n_bytes))


def make_constant(n_bytes: int) -> bytes:
    return b"A" * n_bytes


def make_pcm_int16(n_bytes: int) -> bytes:
    n = n_bytes // 2
    return struct.pack(f"<{n}h", *(int(20000 * math.sin(0.05 * i)) for i in range(n)))


SAMPLES = [
    ("text",        make_text,         2 * 1024 * 1024),
    ("numeric_f64", make_numeric_f64,  2 * 1024 * 1024),
    ("numeric_i32", make_numeric_i32,  2 * 1024 * 1024),
    ("pcm_int16",   make_pcm_int16,    2 * 1024 * 1024),
    ("random",      make_random,       1 * 1024 * 1024),
    ("constant",    make_constant,     2 * 1024 * 1024),
]


def main():
    print(f"{'Input':<14} {'Size MB':>8} {'Ratio':>8} {'Comp MB/s':>11} {'Decomp MB/s':>13}")
    print("-" * 60)
    for name, factory, size in SAMPLES:
        data = factory(size)
        mb = len(data) / (1024 * 1024)

        t0 = time.perf_counter()
        for _ in range(3):
            c = compress(data)
        t_c = (time.perf_counter() - t0) / 3

        t0 = time.perf_counter()
        for _ in range(3):
            back = decompress(c)
        t_d = (time.perf_counter() - t0) / 3

        ok = back == data
        ratio = len(c) / max(1, len(data))
        status = "" if ok else "  FAIL"
        print(f"{name:<14} {mb:>8.2f} {ratio:>8.4f} "
              f"{mb / t_c:>11.1f} {mb / t_d:>13.1f}{status}")


if __name__ == "__main__":
    main()
