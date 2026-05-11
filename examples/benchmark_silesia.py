"""Silesia benchmark: OmniComp vs zstd (unified outputs).

Writes two CSV files (default paths under docs/):

  docs/silesia_vs_zstd_metrics.csv
    One row per level on the full Silesia blob (concatenated files).
    Columns include ratio, wall times (seconds, best-of-``--repeats``) for
    compress/decompress each side, derived MB/s, percentage deltas, and RSS.

  docs/silesia_per_file.csv
    One row per (dataset, level) for each corpus file plus the blob.
    Same metrics and deltas as above.

  RSS columns (each benchmark runs in a fresh ``spawn`` child process):
    ``rss_peak_delta_*_mb`` is ``ru_maxrss`` after the compress+decompress
    work minus ``ru_maxrss`` before (macOS: ``ru_maxrss`` treated as bytes;
    Linux: kilobytes per ``getrusage``). It is an approximate **peak resident
    set growth** attributable to that run, not a full allocator profile.
    ``rss_delta_diff_omni_minus_zstd_mb`` is Omni minus zstd for the same row.

Levels default: 1, 3, 9, 12, 15, 22 (same numeric level for zstd and OmniComp).

Threading:
  ``--threads N`` (default 1): zstd compression uses ``N`` worker threads when ``N>1``;
  OmniComp uses ``n_threads=N`` for block-parallel compression. Decompression stays
  single-threaded for both (lib / binding limits). With default CSV paths and
  ``N>1``, outputs are written as ``docs/silesia_vs_zstd_metrics_<N>t.csv`` and
  ``docs/silesia_per_file_<N>t.csv`` so single-thread baselines are preserved.

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
BLOB_CSV = "docs/silesia_vs_zstd_metrics.csv"
PER_FILE_CSV = "docs/silesia_per_file.csv"


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
    import resource
    import time as time_mod

    import zstandard as zstd

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from omnicomp import compress as omni_compress, decompress as omni_decompress

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
    if dataset == "__blob__":
        data = b"".join((root / f).read_bytes() for f in SILESIA_FILES if (root / f).exists())
    else:
        data = (root / dataset).read_bytes()

    r0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    n = len(data)
    size_mb = mb(n)

    z_threads = 0 if threads <= 1 else threads
    o_threads = max(1, threads)

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
        }
    return parent.recv()


def pct_delta(omni: float, zstd_v: float) -> float:
    if zstd_v == 0 or zstd_v != zstd_v or omni != omni:
        return float("nan")
    return (omni / zstd_v - 1.0) * 100.0


def row_blob(level: int, sdir: Path, repeats: int, threads: int) -> dict:
    z = bench_isolated("zstd", level, sdir, "__blob__", repeats, threads)
    o = bench_isolated("omni", level, sdir, "__blob__", repeats, threads)
    size_mb = mb(
        sum((sdir / f).stat().st_size for f in SILESIA_FILES if (sdir / f).exists())
    )
    return {
        "n_threads": threads,
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
    "level",
    "dataset",
    "size_mb",
    "ratio_zstd",
    "ratio_omni",
    "ratio_delta_pct_omni_vs_zstd",
    "comp_zstd_s",
    "comp_omni_s",
    "comp_time_delta_pct_omni_vs_zstd",
    "decomp_zstd_s",
    "decomp_omni_s",
    "decomp_time_delta_pct_omni_vs_zstd",
    "decomp_zstd_mb_s",
    "decomp_omni_mb_s",
    "decomp_delta_pct_omni_vs_zstd",
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
                if isinstance(v, int) and k == "n_threads":
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
        help="zstd compression threads and OmniComp n_threads (>=1).",
    )
    ap.add_argument("--blob-csv", default="")
    ap.add_argument("--per-file-csv", default="")
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

    threads = max(1, args.threads)
    if not args.blob_csv:
        if threads <= 1:
            args.blob_csv = BLOB_CSV
        else:
            args.blob_csv = str(
                Path(BLOB_CSV).with_name(
                    f"{Path(BLOB_CSV).stem}_{threads}t.csv"
                )
            )
    if not args.per_file_csv:
        if threads <= 1:
            args.per_file_csv = PER_FILE_CSV
        else:
            args.per_file_csv = str(
                Path(PER_FILE_CSV).with_name(
                    f"{Path(PER_FILE_CSV).stem}_{threads}t.csv"
                )
            )

    blob_rows: list[dict] = []
    per_rows: list[dict] = []

    print(f"OmniComp Silesia benchmark (unified CSV)")
    print(f"  silesia dir : {sdir}")
    print(f"  levels      : {levels}")
    print(f"  threads     : {threads}")
    print(f"  repeats     : {args.repeats}")
    print(f"  blob CSV    : {args.blob_csv}")
    print(f"  per-file CSV: {args.per_file_csv}")

    if not args.no_blob:
        print("\n" + "=" * 78)
        print("BLOB (metrics CSV)")
        print("=" * 78)
        for lvl in levels:
            print(f"  level {lvl} ...", flush=True)
            blob_rows.append(row_blob(lvl, sdir, args.repeats, threads))
            r = blob_rows[-1]
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
                per_rows.append(row_per_file(lvl, sdir, fname, args.repeats, threads))
            print(f"  [ok] {fname}")

        if not args.no_blob and blob_rows:
            for r in blob_rows:
                per_rows.append(dict(r))

    if not args.no_blob:
        write_csv_rows(Path(args.blob_csv), BLOB_FIELDS, blob_rows)
        print(f"\nWrote {args.blob_csv} ({len(blob_rows)} rows)")
    if not args.no_per_file:
        write_csv_rows(Path(args.per_file_csv), BLOB_FIELDS, per_rows)
        print(f"Wrote {args.per_file_csv} ({len(per_rows)} rows)")
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
