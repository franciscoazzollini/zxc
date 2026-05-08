"""Silesia benchmark for OmniComp vs zstd.

Runs every file in the Silesia corpus AND the concatenated blob, reporting:
  - ratio (zstd convention: original / compressed)
  - compression MB/s (best of N runs)
  - decompression MB/s (best of N runs)
  - whether the round-trip is bit-exact (PASS / FAIL)

By default it expects the corpus at /tmp/silesia. Pass --silesia-dir to override.

Examples:
    python3 examples/benchmark_silesia.py --silesia-dir /tmp/silesia
    python3 examples/benchmark_silesia.py --threads 1 --algos zstd-1,zstd-3,omnicomp
    python3 examples/benchmark_silesia.py --files mozilla,mr,osdb     # focus on the failing ones

Download the corpus once with:
    mkdir -p /tmp/silesia && cd /tmp/silesia
    for f in dickens mozilla mr nci ooffice osdb reymont samba sao webster x-ray xml; do
        curl -fsSL -o $f.zip \\
            https://github.com/MiloszKrajewski/SilesiaCorpus/raw/master/$f.zip
        unzip -q -o $f.zip
    done
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Make ``import omnicomp`` work without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zstandard as zstd
from omnicomp import compress as omni_compress, decompress as omni_decompress


SILESIA_FILES = [
    "dickens", "mozilla", "mr", "nci", "ooffice", "osdb",
    "reymont", "samba", "sao", "webster", "x-ray", "xml",
]


def time_run(fn, *args, repeats: int):
    best = float("inf")
    out = None
    for _ in range(repeats):
        t = time.perf_counter()
        out = fn(*args)
        dt = time.perf_counter() - t
        if dt < best:
            best = dt
    return out, best


def make_algos(threads: int):
    """Return [(name, comp_fn, decomp_fn), ...] for all algorithms.

    threads=0 means single-threaded for zstd, single-threaded for OmniComp.
    threads>0 enables that many worker threads.
    """
    z_threads = 0 if threads in (0, 1) else threads
    o_threads = 1 if threads in (0, 1) else threads

    algos = []
    for level, label in [(-3, "zstd --fast=3"), (-1, "zstd --fast=1"),
                         (1, "zstd -1"), (3, "zstd -3"), (9, "zstd -9")]:
        cctx = zstd.ZstdCompressor(level=level, threads=z_threads)
        dctx = zstd.ZstdDecompressor()
        algos.append((label,
                      lambda d, c=cctx: c.compress(d),
                      lambda c, d=dctx: d.decompress(c)))
    for level, label in [(1, "OmniComp L1"), (3, "OmniComp L3"),
                         (6, "OmniComp L6"), (9, "OmniComp L9")]:
        algos.append((label,
                      lambda d, t=o_threads, l=level: omni_compress(d, level=l, n_threads=t),
                      omni_decompress))
    return algos


def filter_algos(algos, names):
    if not names:
        return algos
    wanted = set(names.split(","))
    return [a for a in algos if a[0].replace(" ", "").replace("-", "") in
            {w.replace(" ", "").replace("-", "") for w in wanted}
            or a[0] in wanted]


def bench_one(name, data, algos, repeats):
    print(f"\n--- {name}  ({len(data) / (1024 * 1024):.2f} MB) ---")
    print(f"  {'algo':<22} {'ratio':>7} {'comp MB/s':>11} {'decomp MB/s':>13}  status")
    for algo_name, comp_fn, decomp_fn in algos:
        try:
            comp, t_c = time_run(comp_fn, data, repeats=repeats)
            back, t_d = time_run(decomp_fn, comp, repeats=repeats)
        except Exception as e:
            print(f"  {algo_name:<22}  ERROR: {type(e).__name__}: {e}")
            continue
        mb = len(data) / (1024 * 1024)
        ratio = len(data) / max(1, len(comp))  # zstd convention
        ok = back == data
        status = "OK" if ok else "FAIL (lossy!)"
        print(f"  {algo_name:<22} {ratio:>7.3f} {mb / t_c:>11.1f} {mb / t_d:>13.1f}  {status}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--silesia-dir", default="/tmp/silesia")
    ap.add_argument("--threads", type=int, default=1,
                    help="0/1 = single-threaded; >1 = that many threads.")
    ap.add_argument("--algos", default="",
                    help="comma-separated subset (e.g. 'zstd -1,OmniComp')")
    ap.add_argument("--files", default=",".join(SILESIA_FILES),
                    help="comma-separated subset of Silesia files")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--no-blob", action="store_true",
                    help="skip the concatenated blob run")
    ap.add_argument("--no-per-file", action="store_true",
                    help="skip per-file runs (only blob)")
    args = ap.parse_args()

    sdir = Path(args.silesia_dir)
    if not sdir.exists():
        print(f"Silesia dir not found: {sdir}", file=sys.stderr)
        return 1

    algos = filter_algos(make_algos(args.threads), args.algos)
    files = [f for f in args.files.split(",") if f]

    print(f"OmniComp Silesia benchmark")
    print(f"  silesia dir : {sdir}")
    print(f"  threads     : {args.threads}")
    print(f"  algorithms  : {[a[0] for a in algos]}")

    if not args.no_per_file:
        print("\n" + "=" * 78)
        print("PER-FILE")
        print("=" * 78)
        for fname in files:
            path = sdir / fname
            if not path.exists():
                print(f"  (missing: {path})")
                continue
            bench_one(fname, path.read_bytes(), algos, args.repeats)

    if not args.no_blob:
        print("\n" + "=" * 78)
        print("FULL SILESIA BLOB (concatenated)")
        print("=" * 78)
        blob = b"".join((sdir / f).read_bytes() for f in files
                        if (sdir / f).exists())
        bench_one("silesia.tar", blob, algos, args.repeats)

    return 0


if __name__ == "__main__":
    sys.exit(main())
