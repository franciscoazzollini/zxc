"""Silesia benchmark: OmniComp vs zstd (unified outputs).

Writes two CSV files (default paths under docs/):

  docs/silesia_vs_zstd_metrics.csv
    One row per level on the full Silesia blob (concatenated files).
    Columns include ratio / decompression speed for zstd and OmniComp,
    percentage deltas, and peak RSS growth during the run (child process).

  docs/silesia_per_file.csv
    One row per (dataset, level) for each corpus file plus the blob.
    Same metrics and deltas as above.

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
BLOB_CSV = "docs/silesia_vs_zstd_metrics.csv"
PER_FILE_CSV = "docs/silesia_per_file.csv"


def mb(n: int) -> float:
    return n / (1024.0 * 1024.0)


def _child_bench(conn, kind: str, level: int, sdir: str, dataset: str, repeats: int) -> None:
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

    if kind == "zstd":
        cctx = zstd.ZstdCompressor(level=level, threads=0)
        dctx = zstd.ZstdDecompressor()
        comp, t_c = run_best(lambda: cctx.compress(data), repeats)
        back, t_d = run_best(lambda: dctx.decompress(comp), repeats)
    else:
        comp, t_c = run_best(lambda: omni_compress(data, level=level, n_threads=1), repeats)
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
        "comp_mb_s": size_mb / t_c,
        "decomp_mb_s": size_mb / t_d,
        "rss_peak_delta_mb": rss_peak_delta_mb,
        "status": "OK" if ok else "FAIL",
    }
    conn.send(out)
    conn.close()


def bench_isolated(kind: str, level: int, sdir: Path, dataset: str, repeats: int) -> dict:
    ctx = mp.get_context("spawn")
    parent, child = mp.Pipe(duplex=False)
    p = ctx.Process(
        target=_child_bench,
        args=(child, kind, level, str(sdir), dataset, repeats),
    )
    p.start()
    p.join(timeout=3600)
    if p.exitcode != 0:
        p.kill()
        return {
            "ratio": float("nan"),
            "comp_mb_s": float("nan"),
            "decomp_mb_s": float("nan"),
            "rss_peak_delta_mb": float("nan"),
            "status": f"ERROR exit={p.exitcode}",
        }
    if not parent.poll(0.1):
        return {
            "ratio": float("nan"),
            "comp_mb_s": float("nan"),
            "decomp_mb_s": float("nan"),
            "rss_peak_delta_mb": float("nan"),
            "status": "ERROR no result",
        }
    return parent.recv()


def pct_delta(omni: float, zstd_v: float) -> float:
    if zstd_v == 0 or zstd_v != zstd_v or omni != omni:
        return float("nan")
    return (omni / zstd_v - 1.0) * 100.0


def row_blob(level: int, sdir: Path, repeats: int) -> dict:
    z = bench_isolated("zstd", level, sdir, "__blob__", repeats)
    o = bench_isolated("omni", level, sdir, "__blob__", repeats)
    size_mb = mb(
        sum((sdir / f).stat().st_size for f in SILESIA_FILES if (sdir / f).exists())
    )
    return {
        "level": level,
        "dataset": "silesia_blob",
        "size_mb": size_mb,
        "ratio_zstd": z["ratio"],
        "ratio_omni": o["ratio"],
        "ratio_delta_pct_omni_vs_zstd": pct_delta(o["ratio"], z["ratio"]),
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


def row_per_file(level: int, sdir: Path, dataset: str, repeats: int) -> dict:
    z = bench_isolated("zstd", level, sdir, dataset, repeats)
    o = bench_isolated("omni", level, sdir, dataset, repeats)
    p = sdir / dataset
    size_mb = mb(p.stat().st_size) if p.exists() else 0.0
    return {
        "level": level,
        "dataset": dataset,
        "size_mb": size_mb,
        "ratio_zstd": z["ratio"],
        "ratio_omni": o["ratio"],
        "ratio_delta_pct_omni_vs_zstd": pct_delta(o["ratio"], z["ratio"]),
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
    "level",
    "dataset",
    "size_mb",
    "ratio_zstd",
    "ratio_omni",
    "ratio_delta_pct_omni_vs_zstd",
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
                if isinstance(v, float):
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
    ap.add_argument("--blob-csv", default=BLOB_CSV)
    ap.add_argument("--per-file-csv", default=PER_FILE_CSV)
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

    blob_rows: list[dict] = []
    per_rows: list[dict] = []

    print(f"OmniComp Silesia benchmark (unified CSV)")
    print(f"  silesia dir : {sdir}")
    print(f"  levels      : {levels}")
    print(f"  repeats     : {args.repeats}")
    print(f"  blob CSV    : {args.blob_csv}")
    print(f"  per-file CSV: {args.per_file_csv}")

    if not args.no_blob:
        print("\n" + "=" * 78)
        print("BLOB (metrics CSV)")
        print("=" * 78)
        for lvl in levels:
            print(f"  level {lvl} ...", flush=True)
            blob_rows.append(row_blob(lvl, sdir, args.repeats))
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
                per_rows.append(row_per_file(lvl, sdir, fname, args.repeats))
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
