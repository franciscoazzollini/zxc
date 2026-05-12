"""Silesia benchmark: OmniComp vs zstd (unified outputs).

Default files (single ``--thread-configs`` run, ``n_threads=1``):

  docs/silesia_vs_zstd_metrics.csv — blob summary rows.
  docs/silesia_per_file.csv — per-file rows plus blob rows.

Multiple thread counts (e.g. ``--thread-configs 1,8``) write suffixed
``docs/silesia_*_{N}t.csv`` so runs do not overwrite each other.

With ``--threads 1``, OmniComp decode uses **sequential block decode** in C
(``OMNICOMP_OMN8_DECOMP_SINGLE_THREAD=1``) so per-file decompress MB/s is
comparable to single-threaded zstd. The CSV column ``omni_seq_block_decode``
is **1** when that env is active for the Omni worker (proof of fair 1-CPU
decode). Column ``decomp_mb_s_ratio_omni_over_zstd`` is Omni/zstd throughput
(e.g. ``3.1`` ⇒ Omni ~3× faster — matches ~+210 % on ``decomp_delta_pct``).
Without sequential decode, large files could still beat zstd by a large factor,
but not from pthread parallelism alone.

Use ``--thread-configs`` or ``--threads`` when passing explicit ``--blob-csv``
/ ``--per-file-csv``.

For the ``silesia_blob`` row, both codecs compress **each corpus file**
independently, then **decompress all streams in parallel** with a
``ThreadPoolExecutor`` (width ``min(--threads, num_files)``). Each zstd stream is
still single-threaded internally; parallelism matches OmniComp-style multicore
decode because libzstd cannot parallelise one frame. OmniComp sets
``OMNICOMP_OMN8_DECOMP_SINGLE_THREAD=1`` so **block-level pthread decode is off**
inside each container while the outer pool supplies parallelism — avoiding
double-scaling threads vs zstd.

Per-file CSV rows still benchmark **one file at a time** (no outer pool).

Default is **1 compression thread** for headline CSVs (``--threads`` /
``--thread-configs`` to use more).

Leading columns (then absolute timings, MB/s, RSS, status): ``n_threads``,
``level``, ``dataset``, ``size_mb``, ``ratio_delta_pct_omni_vs_zstd``,
``decomp_delta_pct_omni_vs_zstd``, ``decomp_time_delta_pct_omni_vs_zstd``.

RSS columns (each benchmark runs in a fresh ``spawn`` child process):
  ``rss_peak_delta_*_mb`` is ``ru_maxrss`` after the compress+decompress
  work minus ``ru_maxrss`` before (macOS: ``ru_maxrss`` treated as bytes;
  Linux: kilobytes per ``getrusage``). ``rss_delta_diff_omni_minus_zstd_mb`` is
  Omni minus zstd for the same row.

Levels default: 1, 3, 9, 12, 15, 22 (same numeric level for zstd and OmniComp).

Download the corpus once with:
    mkdir -p /tmp/silesia && cd /tmp/silesia
    for f in dickens mozilla mr nci ooffice osdb reymont samba sao webster x-ray xml; do
        curl -fsSL -o $f.bz2 \\
            "https://sun.aei.polsl.pl/~sdeor/corpus/$f.bz2"
        bzip2 -d -f $f.bz2
    done
"""
from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zstandard as zstd

SILESIA_FILES = [
    "dickens", "mozilla", "mr", "nci", "ooffice", "osdb",
    "reymont", "samba", "sao", "webster", "x-ray", "xml",
]

DEFAULT_LEVELS = [1, 3, 9, 12, 15, 22]
DEFAULT_THREAD_CONFIGS = "1"


def default_blob_csv(threads: int, total_runs: int) -> str:
    if total_runs > 1:
        return f"docs/silesia_vs_zstd_metrics_{threads}t.csv"
    return "docs/silesia_vs_zstd_metrics.csv"


def default_per_file_csv(threads: int, total_runs: int) -> str:
    if total_runs > 1:
        return f"docs/silesia_per_file_{threads}t.csv"
    return "docs/silesia_per_file.csv"


def mb(n: int) -> float:
    return n / (1024.0 * 1024.0)


def _child_bench(
    conn,
    kind: str,
    level: int,
    sdir: str,
    dataset: str,
    repeats: int,
    threads: int,
) -> None:
    """Run in a fresh process: load data from disk, bench, send one dict."""
    import concurrent.futures
    import os
    import resource
    import time as time_mod

    # Before importing omnicomp: OMN8 pthread decode off when benchmarking 1 CPU
    # or blob outer-parallel mode (see docstring).
    if threads <= 1:
        os.environ["OMNICOMP_OMN8_DECOMP_SINGLE_THREAD"] = "1"
    elif dataset != "__blob__":
        os.environ.pop("OMNICOMP_OMN8_DECOMP_SINGLE_THREAD", None)
    if dataset == "__blob__":
        os.environ["OMNICOMP_OMN8_DECOMP_SINGLE_THREAD"] = "1"

    import zstandard as zstd

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from omnicomp import compress as omni_compress, decompress as omni_decompress

    def omni_seq_block_decode_flag() -> int:
        return 1 if os.environ.get("OMNICOMP_OMN8_DECOMP_SINGLE_THREAD") == "1" else 0

    def run_best(fn, n):
        best_t = float("inf")
        out = None
        for _ in range(n):
            t0 = time_mod.perf_counter()
            out = fn()
            dt = time_mod.perf_counter() - t0
            if dt < best_t:
                best_t = dt
        return out, best_t

    root = Path(sdir)
    z_threads = 0 if threads <= 1 else threads
    o_threads = max(1, threads)

    if dataset == "__blob__":
        parts_bytes = []
        for fn in SILESIA_FILES:
            p = root / fn
            if p.exists():
                parts_bytes.append(p.read_bytes())
        if not parts_bytes:
            conn.send(
                {
                    "ratio": float("nan"),
                    "comp_s": float("nan"),
                    "decomp_s": float("nan"),
                    "comp_mb_s": float("nan"),
                    "decomp_mb_s": float("nan"),
                    "rss_peak_delta_mb": float("nan"),
                    "status": "ERROR empty blob",
                    "n_threads": threads,
                }
            )
            conn.close()
            return
        data = b"".join(parts_bytes)
        r0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        n = len(data)
        size_mb = mb(n)
        workers = min(max(1, threads), len(parts_bytes))
        inner_o = max(1, o_threads // workers)

        if kind == "zstd":
            # Compress each corpus file one after another with the full zstd
            # multi-threaded compressor. Parallel decode uses one ZstdDecompressor
            # per task (libzstd decompress contexts are not safe across threads).
            def compress_blob_zstd():
                out = []
                for blob in parts_bytes:
                    cctx = zstd.ZstdCompressor(level=level, threads=z_threads)
                    out.append(cctx.compress(blob))
                return out

            def decompress_parallel(comps: list) -> bytes:
                def dec(c: bytes) -> bytes:
                    return zstd.ZstdDecompressor().decompress(c)

                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                    outs = list(ex.map(dec, comps))
                return b"".join(outs)

            comps, t_c = run_best(compress_blob_zstd, repeats)
            comp_len = sum(len(x) for x in comps)
            back, t_d = run_best(lambda: decompress_parallel(comps), repeats)
        else:

            def compress_parallel_o():
                def enc(blob: bytes) -> bytes:
                    return omni_compress(blob, level=level, n_threads=inner_o)

                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                    return list(ex.map(enc, parts_bytes))

            def decompress_parallel_o(comps: list) -> bytes:
                def dec(c: bytes) -> bytes:
                    return omni_decompress(c)

                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                    outs = list(ex.map(dec, comps))
                return b"".join(outs)

            comps, t_c = run_best(compress_parallel_o, repeats)
            comp_len = sum(len(x) for x in comps)
            back, t_d = run_best(lambda: decompress_parallel_o(comps), repeats)

        r1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            rss0 = r0 / (1024.0 * 1024.0)
            rss1 = r1 / (1024.0 * 1024.0)
        else:
            rss0 = r0 / 1024.0
            rss1 = r1 / 1024.0
        rss_peak_delta_mb = max(0.0, rss1 - rss0)

        ok = back == data
        ratio = n / max(1, comp_len)
        out = {
            "ratio": ratio,
            "comp_s": t_c,
            "decomp_s": t_d,
            "comp_mb_s": size_mb / t_c,
            "decomp_mb_s": size_mb / t_d,
            "rss_peak_delta_mb": rss_peak_delta_mb,
            "status": "OK" if ok else "FAIL",
            "n_threads": threads,
        }
        if kind == "omni":
            out["omni_seq_block_decode"] = omni_seq_block_decode_flag()
        conn.send(out)
        conn.close()
        return

    data = (root / dataset).read_bytes()

    r0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    n = len(data)
    size_mb = mb(n)

    if kind == "zstd":
        cctx = zstd.ZstdCompressor(level=level, threads=z_threads)
        dctx = zstd.ZstdDecompressor()
        comp, t_c = run_best(lambda: cctx.compress(data), repeats)
        back, t_d = run_best(lambda: dctx.decompress(comp), repeats)
    else:
        comp, t_c = run_best(
            lambda: omni_compress(data, level=level, n_threads=o_threads),
            repeats,
        )
        back, t_d = run_best(lambda: omni_decompress(comp), repeats)

    r1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        rss0 = r0 / (1024.0 * 1024.0)
        rss1 = r1 / (1024.0 * 1024.0)
    else:
        rss0 = r0 / 1024.0
        rss1 = r1 / 1024.0
    rss_peak_delta_mb = max(0.0, rss1 - rss0)

    ok = back == data
    ratio = n / max(1, len(comp))
    out = {
        "ratio": ratio,
        "comp_s": t_c,
        "decomp_s": t_d,
        "comp_mb_s": size_mb / t_c,
        "decomp_mb_s": size_mb / t_d,
        "rss_peak_delta_mb": rss_peak_delta_mb,
        "status": "OK" if ok else "FAIL",
        "n_threads": threads,
    }
    if kind == "omni":
        out["omni_seq_block_decode"] = omni_seq_block_decode_flag()
    conn.send(out)
    conn.close()


def bench_isolated(
    kind: str,
    level: int,
    sdir: Path,
    dataset: str,
    repeats: int,
    threads: int,
) -> dict:
    ctx = mp.get_context("spawn")
    parent, child = mp.Pipe(duplex=False)
    p = ctx.Process(
        target=_child_bench,
        args=(child, kind, level, str(sdir), dataset, repeats, threads),
    )
    p.start()
    p.join(timeout=3600)
    if p.exitcode != 0:
        p.kill()
        return {
            "ratio": float("nan"),
            "comp_s": float("nan"),
            "decomp_s": float("nan"),
            "comp_mb_s": float("nan"),
            "decomp_mb_s": float("nan"),
            "rss_peak_delta_mb": float("nan"),
            "status": f"ERROR exit={p.exitcode}",
            "n_threads": threads,
            "omni_seq_block_decode": 0,
        }
    if not parent.poll(0.1):
        return {
            "ratio": float("nan"),
            "comp_s": float("nan"),
            "decomp_s": float("nan"),
            "comp_mb_s": float("nan"),
            "decomp_mb_s": float("nan"),
            "rss_peak_delta_mb": float("nan"),
            "status": "ERROR no result",
            "n_threads": threads,
            "omni_seq_block_decode": 0,
        }
    return parent.recv()


def pct_delta(omni: float, zstd_v: float) -> float:
    if zstd_v == 0 or zstd_v != zstd_v or omni != omni:
        return float("nan")
    return (omni / zstd_v - 1.0) * 100.0


def decomp_mb_s_ratio_omni_over_zstd(o_mb: float, z_mb: float) -> float:
    """Plain ratio Omni/zstd decompress throughput (e.g. 3.0 ⇒ triple zstd MB/s)."""
    if z_mb == 0 or z_mb != z_mb or o_mb != o_mb:
        return float("nan")
    return o_mb / z_mb


def row_blob(level: int, sdir: Path, repeats: int, threads: int) -> dict:
    z = bench_isolated("zstd", level, sdir, "__blob__", repeats, threads)
    o = bench_isolated("omni", level, sdir, "__blob__", repeats, threads)
    size_mb = mb(
        sum((sdir / f).stat().st_size for f in SILESIA_FILES if (sdir / f).exists())
    )
    return {
        "n_threads": threads,
        "omni_seq_block_decode": int(o.get("omni_seq_block_decode", 0)),
        "level": level,
        "dataset": "silesia_blob",
        "size_mb": size_mb,
        "ratio_zstd": z["ratio"],
        "ratio_omni": o["ratio"],
        "ratio_delta_pct_omni_vs_zstd": pct_delta(o["ratio"], z["ratio"]),
        "comp_zstd_s": z["comp_s"],
        "comp_omni_s": o["comp_s"],
        "comp_time_delta_pct_omni_vs_zstd": pct_delta(o["comp_s"], z["comp_s"]),
        "decomp_zstd_s": z["decomp_s"],
        "decomp_omni_s": o["decomp_s"],
        "decomp_time_delta_pct_omni_vs_zstd": pct_delta(o["decomp_s"], z["decomp_s"]),
        "decomp_mb_s_ratio_omni_over_zstd": decomp_mb_s_ratio_omni_over_zstd(
            o["decomp_mb_s"], z["decomp_mb_s"]
        ),
        "decomp_zstd_mb_s": z["decomp_mb_s"],
        "decomp_omni_mb_s": o["decomp_mb_s"],
        "decomp_delta_pct_omni_vs_zstd": pct_delta(o["decomp_mb_s"], z["decomp_mb_s"]),
        "comp_zstd_mb_s": z["comp_mb_s"],
        "comp_omni_mb_s": o["comp_mb_s"],
        "comp_delta_pct_omni_vs_zstd": pct_delta(o["comp_mb_s"], z["comp_mb_s"]),
        "rss_peak_delta_zstd_mb": z["rss_peak_delta_mb"],
        "rss_peak_delta_omni_mb": o["rss_peak_delta_mb"],
        "rss_delta_diff_omni_minus_zstd_mb": o["rss_peak_delta_mb"] - z["rss_peak_delta_mb"],
        "status_zstd": z["status"],
        "status_omni": o["status"],
    }


def row_per_file(level: int, sdir: Path, dataset: str, repeats: int, threads: int) -> dict:
    z = bench_isolated("zstd", level, sdir, dataset, repeats, threads)
    o = bench_isolated("omni", level, sdir, dataset, repeats, threads)
    p = sdir / dataset
    size_mb = mb(p.stat().st_size) if p.exists() else 0.0
    return {
        "n_threads": threads,
        "omni_seq_block_decode": int(o.get("omni_seq_block_decode", 0)),
        "level": level,
        "dataset": dataset,
        "size_mb": size_mb,
        "ratio_zstd": z["ratio"],
        "ratio_omni": o["ratio"],
        "ratio_delta_pct_omni_vs_zstd": pct_delta(o["ratio"], z["ratio"]),
        "comp_zstd_s": z["comp_s"],
        "comp_omni_s": o["comp_s"],
        "comp_time_delta_pct_omni_vs_zstd": pct_delta(o["comp_s"], z["comp_s"]),
        "decomp_zstd_s": z["decomp_s"],
        "decomp_omni_s": o["decomp_s"],
        "decomp_time_delta_pct_omni_vs_zstd": pct_delta(o["decomp_s"], z["decomp_s"]),
        "decomp_mb_s_ratio_omni_over_zstd": decomp_mb_s_ratio_omni_over_zstd(
            o["decomp_mb_s"], z["decomp_mb_s"]
        ),
        "decomp_zstd_mb_s": z["decomp_mb_s"],
        "decomp_omni_mb_s": o["decomp_mb_s"],
        "decomp_delta_pct_omni_vs_zstd": pct_delta(o["decomp_mb_s"], z["decomp_mb_s"]),
        "comp_zstd_mb_s": z["comp_mb_s"],
        "comp_omni_mb_s": o["comp_mb_s"],
        "comp_delta_pct_omni_vs_zstd": pct_delta(o["comp_mb_s"], z["comp_mb_s"]),
        "rss_peak_delta_zstd_mb": z["rss_peak_delta_mb"],
        "rss_peak_delta_omni_mb": o["rss_peak_delta_mb"],
        "rss_delta_diff_omni_minus_zstd_mb": o["rss_peak_delta_mb"] - z["rss_peak_delta_mb"],
        "status_zstd": z["status"],
        "status_omni": o["status"],
    }


BLOB_FIELDS = [
    "n_threads",
    "omni_seq_block_decode",
    "level",
    "dataset",
    "size_mb",
    "ratio_delta_pct_omni_vs_zstd",
    "decomp_delta_pct_omni_vs_zstd",
    "decomp_time_delta_pct_omni_vs_zstd",
    "decomp_mb_s_ratio_omni_over_zstd",
    "ratio_zstd",
    "ratio_omni",
    "comp_zstd_s",
    "comp_omni_s",
    "comp_time_delta_pct_omni_vs_zstd",
    "decomp_zstd_s",
    "decomp_omni_s",
    "decomp_zstd_mb_s",
    "decomp_omni_mb_s",
    "comp_zstd_mb_s",
    "comp_omni_mb_s",
    "comp_delta_pct_omni_vs_zstd",
    "rss_peak_delta_zstd_mb",
    "rss_peak_delta_omni_mb",
    "rss_delta_diff_omni_minus_zstd_mb",
    "status_zstd",
    "status_omni",
]


def write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            line = {}
            for k in fieldnames:
                v = r[k]
                if isinstance(v, int):
                    line[k] = str(v)
                elif isinstance(v, float):
                    line[k] = f"{v:.6f}" if v == v else ""
                else:
                    line[k] = v
            w.writerow(line)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--silesia-dir", default="/tmp/silesia")
    ap.add_argument("--levels", default=",".join(str(x) for x in DEFAULT_LEVELS),
                    help="comma-separated zstd/OmniComp levels (same number for both)")
    ap.add_argument("--repeats", type=int, default=2,
                    help="best-of-N timing inside each isolated run")
    ap.add_argument(
        "--threads",
        type=int,
        default=1,
        help="thread count when --thread-configs is omitted (default 1).",
    )
    ap.add_argument(
        "--thread-configs",
        default=DEFAULT_THREAD_CONFIGS,
        help=(
            "comma-separated thread counts to run sequentially "
            f"(default: {DEFAULT_THREAD_CONFIGS}). Multiple runs use suffixed "
            "docs/silesia_*_Nt.csv; a single run uses docs/silesia_*.csv."
        ),
    )
    ap.add_argument(
        "--blob-csv",
        default=None,
        help="blob metrics path (only with a single thread configuration)",
    )
    ap.add_argument(
        "--per-file-csv",
        default=None,
        help="per-file metrics path (only with a single thread configuration)",
    )
    ap.add_argument("--no-blob", action="store_true")
    ap.add_argument("--no-per-file", action="store_true")
    args = ap.parse_args()

    sdir = Path(args.silesia_dir)
    if not sdir.exists():
        print(f"Silesia dir not found: {sdir}", file=sys.stderr)
        return 1

    levels = [int(x.strip()) for x in args.levels.split(",") if x.strip()]
    if not levels:
        print("No levels parsed", file=sys.stderr)
        return 1

    tc_raw = (args.thread_configs or "").strip()
    if tc_raw:
        runs = [max(1, int(x.strip())) for x in tc_raw.split(",") if x.strip()]
    else:
        runs = [max(1, args.threads)]
    if not runs:
        print("No thread configs parsed", file=sys.stderr)
        return 1

    if (args.blob_csv is not None or args.per_file_csv is not None) and len(runs) > 1:
        print(
            "Use --blob-csv / --per-file-csv only with a single thread configuration.",
            file=sys.stderr,
        )
        return 1

    total_runs = len(runs)
    for ri, threads in enumerate(runs):
        blob_csv = (
            args.blob_csv
            if args.blob_csv is not None
            else default_blob_csv(threads, total_runs)
        )
        per_csv = (
            args.per_file_csv
            if args.per_file_csv is not None
            else default_per_file_csv(threads, total_runs)
        )

        blob_rows: list[dict] = []
        per_rows: list[dict] = []

        print(f"\n{'#' * 78}")
        print(f"# Run {ri + 1}/{len(runs)}  threads={threads}")
        print(f"{'#' * 78}")
        print(f"OmniComp Silesia benchmark")
        print(f"  silesia dir : {sdir}")
        print(f"  levels      : {levels}")
        print(f"  threads     : {threads}")
        print(f"  repeats     : {args.repeats}")
        print(f"  blob CSV    : {blob_csv}")
        print(f"  per-file CSV: {per_csv}")

        if not args.no_blob:
            print("\n" + "=" * 78)
            print("BLOB (metrics CSV)")
            print("=" * 78)
            for lvl in levels:
                print(f"  level {lvl} ...", flush=True)
                r = row_blob(lvl, sdir, args.repeats, threads)
                blob_rows.append(r)
                print(
                    f"    zstd ratio={r['ratio_zstd']:.3f} decomp={r['decomp_zstd_mb_s']:.0f} MB/s | "
                    f"omni ratio={r['ratio_omni']:.3f} decomp={r['decomp_omni_mb_s']:.0f} MB/s | "
                    f"{r['status_zstd']}/{r['status_omni']}"
                )

        if not args.no_per_file:
            print("\n" + "=" * 78)
            print("PER-FILE")
            print("=" * 78)
            for fname in SILESIA_FILES:
                path = sdir / fname
                if not path.exists():
                    print(f"  (missing: {path})")
                    continue
                for lvl in levels:
                    print(f"  {fname} L{lvl} ...", flush=True)
                    row = row_per_file(lvl, sdir, fname, args.repeats, threads)
                    per_rows.append(row)
                print(f"  [ok] {fname}")

        if not args.no_per_file and not args.no_blob and blob_rows:
            for r in blob_rows:
                per_rows.append(dict(r))

        if not args.no_blob:
            write_csv_rows(Path(blob_csv), BLOB_FIELDS, blob_rows)
            print(f"\nWrote {blob_csv} ({len(blob_rows)} rows)")
        if not args.no_per_file:
            write_csv_rows(Path(per_csv), BLOB_FIELDS, per_rows)
            print(f"Wrote {per_csv} ({len(per_rows)} rows)")
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
