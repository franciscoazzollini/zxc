"""System-level validation tests for OmniComp.

These are practical pass/fail checks for the dimensions requested in the
validation matrix:
  - multiple corpus
  - memory used
  - real dataset (if provided)
  - multithreading
  - small files
  - incompressible data
  - streaming
  - latency

Run directly:
    python3 tests/test_validation_matrix.py

Optional:
    REAL_DATASET_DIR=/path/to/real/files python3 tests/test_validation_matrix.py
"""

from __future__ import annotations

import os
import random
import resource
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnicomp import compress, decompress  # noqa: E402


def _mb(n: int) -> float:
    return n / (1024.0 * 1024.0)


def _run_best(fn, repeats: int = 3):
    best_t = float("inf")
    out = None
    for _ in range(repeats):
        t0 = time.perf_counter()
        out = fn()
        dt = time.perf_counter() - t0
        if dt < best_t:
            best_t = dt
    return out, best_t


def _rss_mb() -> float:
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS returns bytes; Linux returns KB.
    if sys.platform == "darwin":
        return v / (1024.0 * 1024.0)
    return v / 1024.0


def test_multiple_corpus():
    corpus = {
        "text": (b"hello world\n" * 20000),
        "structured": (b'{"k":1,"v":"x"}\n' * 30000),
        "numeric-ish": b"".join((i % 4096).to_bytes(2, "little") for i in range(200000)),
        "incompressible": os.urandom(2 * 1024 * 1024),
    }
    for name, data in corpus.items():
        c = compress(data, level=9, n_threads=1)
        b = decompress(c)
        assert b == data, f"{name}: round-trip mismatch"


def test_real_dataset_optional():
    root = os.getenv("REAL_DATASET_DIR", "")
    if not root:
        return
    p = Path(root)
    if not p.exists():
        raise AssertionError(f"REAL_DATASET_DIR does not exist: {p}")
    files = [x for x in p.rglob("*") if x.is_file()][:50]
    if not files:
        return
    for f in files:
        data = f.read_bytes()
        c = compress(data, level=9, n_threads=1)
        b = decompress(c)
        assert b == data, f"{f}: round-trip mismatch"


def test_multithreading():
    data = (b"sensor-001,42.123\n" * 1_000_000) + os.urandom(8 * 1024 * 1024)
    c1, t1 = _run_best(lambda: compress(data, level=9, n_threads=1), repeats=2)
    c8, t8 = _run_best(lambda: compress(data, level=9, n_threads=8), repeats=2)
    assert decompress(c1) == data
    assert decompress(c8) == data
    # Must not catastrophically regress with threading.
    assert t8 < t1 * 1.2, f"threading too slow: 1T={t1:.4f}s 8T={t8:.4f}s"


def test_small_files():
    rng = random.Random(7)
    files = []
    for _ in range(300):
        sz = rng.choice([32, 64, 128, 256, 512, 1024, 2048, 4096])
        files.append(os.urandom(sz))
    t0 = time.perf_counter()
    for f in files:
        c = compress(f, level=9, n_threads=1)
        b = decompress(c)
        assert b == f
    dt_ms = (time.perf_counter() - t0) * 1000.0
    assert dt_ms < 1000.0, f"small-files path unexpectedly slow: {dt_ms:.1f} ms"


def test_incompressible_data():
    data = os.urandom(8 * 1024 * 1024)
    c, tc = _run_best(lambda: compress(data, level=9, n_threads=1), repeats=2)
    b, td = _run_best(lambda: decompress(c), repeats=2)
    assert b == data
    ratio = len(data) / max(1, len(c))
    assert 0.98 <= ratio <= 1.02, f"incompressible ratio unexpected: {ratio:.3f}"
    assert _mb(len(data)) / tc > 50.0, "compress throughput too low"
    assert _mb(len(data)) / td > 200.0, "decompress throughput too low"


def test_streaming_chunked_frames():
    src = os.urandom(2 * 1024 * 1024) + (b"logline-abc\n" * 200000)
    chunk = 128 * 1024
    frames = [compress(src[i:i + chunk], level=9, n_threads=1)
              for i in range(0, len(src), chunk)]
    out = b"".join(decompress(f) for f in frames)
    assert out == src, "streaming frame sequence mismatch"


def test_latency():
    payloads = {
        "4KB": os.urandom(4 * 1024),
        "64KB": os.urandom(64 * 1024),
        "1MB": os.urandom(1024 * 1024),
    }
    for name, data in payloads.items():
        c_ms = []
        d_ms = []
        for _ in range(30):
            t0 = time.perf_counter()
            c = compress(data, level=9, n_threads=1)
            t1 = time.perf_counter()
            b = decompress(c)
            t2 = time.perf_counter()
            assert b == data
            c_ms.append((t1 - t0) * 1000.0)
            d_ms.append((t2 - t1) * 1000.0)
        c95 = sorted(c_ms)[int(0.95 * len(c_ms)) - 1]
        d95 = sorted(d_ms)[int(0.95 * len(d_ms)) - 1]
        # Wide but useful guardrails (avoid flaky CI).
        if name == "1MB":
            assert c95 < 10.0, f"{name} compression p95 too high: {c95:.3f} ms"
            assert d95 < 3.0, f"{name} decompression p95 too high: {d95:.3f} ms"


def test_memory_used():
    before = _rss_mb()
    data = os.urandom(32 * 1024 * 1024)
    c = compress(data, level=9, n_threads=1)
    b = decompress(c)
    assert b == data
    after = _rss_mb()
    delta = max(0.0, after - before)
    # Keep a broad bound: process should not explode in memory for 32MB input.
    assert delta < 1024.0, f"RSS delta too high: {delta:.1f} MB"


def main():
    tests = [
        ("multiple corpus", test_multiple_corpus),
        ("real dataset optional", test_real_dataset_optional),
        ("multithreading", test_multithreading),
        ("small files", test_small_files),
        ("incompressible data", test_incompressible_data),
        ("streaming", test_streaming_chunked_frames),
        ("latency", test_latency),
        ("memory used", test_memory_used),
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
        print(f"{failed} validation test(s) failed.")
        sys.exit(1)
    print("All validation tests passed.")


if __name__ == "__main__":
    main()
